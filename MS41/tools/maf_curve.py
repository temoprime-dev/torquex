#!/usr/bin/env python3
"""Replace the MS41 MAF transfer curve with a documented sensor curve.

The lookup grid is known exactly (256 points, 0.000 ... 5.018 V, step 5.018/255,
declared as static labels in the XDF), so a supplier curve given as
"voltage -> kg/h" can be resampled onto the ECU's own columns without guessing.

What this tool refuses:

* a curve without provenance (sensor name and source are mandatory, so an
  invented curve cannot look like a datasheet);
* a curve that is not a function of voltage (voltages must strictly increase) or
  whose flow is not non-decreasing, since the ECU interpolates between columns;
* flows that do not fit the 16-bit cell at the table's scaling, or exceed the
  allowlist ceiling;
* anything the patch core would refuse - the plan is dry-run through it.

What it reports instead of hiding: how many columns had to be held flat at the
ends of the supplier curve, the largest deviation from the stock curve and where
it is, and whether the new sensor reaches beyond the stock MAF limits (which
would need the load/MAF limit constants reviewed as well).
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
    from MS41.tools.xdf import get_object, load_definition, read_engineering
except ModuleNotFoundError:  # Direct execution from MS41/tools.
    from calibration import (
        DEFAULT_POLICY,
        PatchError,
        Policy,
        load_calibration,
        plan_patches,
    )
    from xdf import get_object, load_definition, read_engineering

MAF_TITLE = "id_maf_tab__v_maf_1__v_maf_2 (1x256)"
MAF_TITLE_SECOND_VIEW = "id_maf_tab__v_maf_1__v_maf_2 (16x16)"
PROVENANCE_KINDS = ("datasheet", "measured", "derived")
SCALE_CEILING_KGH = 65535 * 0.015625  # 16-bit cell at the table's 1/64 scaling


class CurveError(ValueError):
    """Raised when a curve file cannot be used as a transfer function."""


@dataclass(frozen=True)
class Curve:
    sensor: str
    source: str
    provenance: str
    points: tuple[tuple[float, float], ...]
    units: str = "kg/h"

    @property
    def start_voltage(self) -> float:
        return self.points[0][0]

    @property
    def end_voltage(self) -> float:
        return self.points[-1][0]

    @property
    def ceiling(self) -> float:
        return self.points[-1][1]

    def flow_at(self, voltage: float) -> tuple[float, bool]:
        """Linearly interpolated flow, plus whether the result was held flat."""
        if voltage <= self.start_voltage:
            return self.points[0][1], voltage < self.start_voltage
        if voltage >= self.end_voltage:
            return self.points[-1][1], voltage > self.end_voltage
        for (v0, f0), (v1, f1) in zip(self.points, self.points[1:]):
            if v0 <= voltage <= v1:
                fraction = (voltage - v0) / (v1 - v0)
                return f0 + fraction * (f1 - f0), False
        raise CurveError(f"{voltage} V is outside the curve")  # pragma: no cover

    def to_json(self) -> dict[str, Any]:
        return {
            "sensor": self.sensor,
            "source": self.source,
            "provenance": self.provenance,
            "units": self.units,
            "points": [[v, f] for v, f in self.points],
        }


def parse_curve(data: dict[str, Any]) -> Curve:
    if not isinstance(data, dict):
        raise CurveError("curve file must be a JSON object")
    sensor = str(data.get("sensor") or "").strip()
    source = str(data.get("source") or "").strip()
    provenance = str(data.get("provenance") or "").strip().lower()
    if not sensor or not source:
        raise CurveError(
            "PROVENANCE_MISSING: 'sensor' and 'source' are mandatory; a curve without "
            "them cannot be told apart from a guess"
        )
    if provenance not in PROVENANCE_KINDS:
        raise CurveError(
            f"PROVENANCE_MISSING: 'provenance' must be one of {PROVENANCE_KINDS}"
        )
    units = str(data.get("units") or "kg/h").strip()
    if units != "kg/h":
        raise CurveError(f"UNITS_UNSUPPORTED: the MAF table is in kg/h, got {units!r}")

    raw_points = data.get("points")
    if not isinstance(raw_points, list) or len(raw_points) < 3:
        raise CurveError("CURVE_TOO_SHORT: at least three (voltage, flow) points are needed")
    points: list[tuple[float, float]] = []
    for index, point in enumerate(raw_points):
        if (not isinstance(point, (list, tuple)) or len(point) != 2
                or not all(isinstance(value, (int, float)) for value in point)):
            raise CurveError(f"point {index} must be [voltage, flow]")
        voltage, flow = float(point[0]), float(point[1])
        if flow < 0:
            raise CurveError(f"FLOW_NEGATIVE: point {index} has {flow} kg/h")
        if flow > SCALE_CEILING_KGH:
            raise CurveError(
                f"FLOW_ABOVE_SCALE: point {index} has {flow} kg/h, above the "
                f"{SCALE_CEILING_KGH:.2f} kg/h the 16-bit cell can hold"
            )
        points.append((voltage, flow))

    if any(b[0] <= a[0] for a, b in zip(points, points[1:])):
        raise CurveError("VOLTAGE_NOT_INCREASING: voltages must strictly increase")
    if any(b[1] < a[1] for a, b in zip(points, points[1:])):
        raise CurveError(
            "FLOW_DECREASING: the transfer function must be non-decreasing; the ECU "
            "interpolates between columns"
        )
    if points[0][0] < 0 or points[-1][0] > 5.018 + 1e-9:
        raise CurveError("voltage range must stay within 0.000 ... 5.018 V")
    return Curve(sensor=sensor, source=source, provenance=provenance,
                 points=tuple(points), units=units)


def load_curve(path: Path) -> Curve:
    return parse_curve(json.loads(path.read_text(encoding="utf-8")))


def resample(curve: Curve, grid: tuple[float, ...]) -> tuple[list[float], dict[str, int]]:
    """Sample the curve on the ECU's voltage grid.

    Below the supplier curve's first point and above its last one the edge value
    is held; the counts are returned so the report can show how much of the
    curve is assumption rather than datasheet.
    """
    values: list[float] = []
    held_below = 0
    held_above = 0
    for voltage in grid:
        flow, held = curve.flow_at(voltage)
        values.append(flow)
        if held:
            if voltage < curve.start_voltage:
                held_below += 1
            else:
                held_above += 1
    return values, {"held_below": held_below, "held_above": held_above}


@dataclass
class CurveReport:
    status: str
    curve: Curve | None
    grid: tuple[float, ...]
    stock: list[float] = field(default_factory=list)
    requested: list[float] = field(default_factory=list)
    details: dict[str, Any] = field(default_factory=dict)
    findings: list[dict[str, str]] = field(default_factory=list)
    patches: list[dict[str, Any]] = field(default_factory=list)
    source_sha256: str = ""
    plan_status: str = ""

    def to_json(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "plan_status": self.plan_status,
            "source_sha256": self.source_sha256,
            "curve": self.curve.to_json() if self.curve else None,
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


def _deviation(stock: list[float], new: list[float],
               grid: tuple[float, ...]) -> dict[str, Any]:
    absolute = [abs(a - b) for a, b in zip(stock, new)]
    worst_index = max(range(len(absolute)), key=absolute.__getitem__)
    worst_rel = 0.0
    for index, (a, b) in enumerate(zip(stock, new)):
        if a > 1e-9:
            worst_rel = max(worst_rel, abs(b - a) / a * 100.0)
    changed = sum(1 for a, b in zip(stock, new) if abs(a - b) >= 0.015625 / 2)
    return {
        "max_absolute_kgh": round(max(absolute), 4),
        "max_absolute_voltage": round(grid[worst_index], 4),
        "max_relative_percent": round(worst_rel, 2),
        "cells_changed": changed,
        "stock_ceiling_kgh": round(max(stock), 4),
        "new_ceiling_kgh": round(max(new), 4),
    }


def build_report(stock_path: Path, curve: Curve, policy: Policy,
                 symbol: str = MAF_TITLE) -> CurveReport:
    objects, problems = load_definition()
    image, offset, image_type = load_calibration(stock_path)
    obj = get_object(objects, symbol)
    grid = obj.axis_labels("y")
    if len(grid) != obj.element_count:
        raise CurveError(
            f"{symbol}: the XDF declares {len(grid)} axis labels for "
            f"{obj.element_count} columns; the grid is incomplete"
        )
    stock = read_engineering(bytes(image[offset:offset + 0x6000]), obj)
    requested, held = resample(curve, grid)

    report = CurveReport(
        status="READY",
        curve=curve,
        grid=grid,
        stock=stock,
        requested=requested,
        source_sha256=hashlib.sha256(bytes(image)).hexdigest(),
    )
    report.details = {
        "symbol": symbol,
        "address": f"0x{obj.address:04X}",
        "columns": obj.element_count,
        "grid_step_volts": round(grid[-1] / (len(grid) - 1), 8),
        "grid_label_max_error_volts": round(
            max(abs(grid[i] - grid[-1] / (len(grid) - 1) * i) for i in range(len(grid))), 6),
        "grid_full_scale_volts": grid[-1],
        "held_below_columns": held["held_below"],
        "held_above_columns": held["held_above"],
        "deviation_vs_stock": _deviation(stock, requested, grid),
    }
    if held["held_below"] or held["held_above"]:
        report.findings.append(_finding(
            "CURVE_EXTRAPOLATED", "warning",
            f"{held['held_below']} column(s) below {curve.start_voltage:.3f} V and "
            f"{held['held_above']} column(s) above {curve.end_voltage:.3f} V were held "
            "at the curve's edge values; that part of the transfer function is an "
            "assumption, not data",
        ))

    ceiling_delta = max(requested) - max(stock)
    if ceiling_delta > 0:
        report.findings.append(_finding(
            "CEILING_ABOVE_STOCK", "warning",
            f"the new curve reaches {max(requested):.1f} kg/h against the stock "
            f"{max(stock):.1f} kg/h; MAF and load limit constants (c_maf_max*) must be "
            "reviewed in the same order, otherwise the extra range is unreachable",
        ))

    requests = [{"symbol": symbol, "index": index, "value": value}
                for index, value in enumerate(requested)]
    plan = plan_patches(image, offset, image_type, objects, problems, requests, policy)
    report.plan_status = plan.status
    _extend_findings(report.findings, [
        _finding(finding.code, finding.severity, finding.message)
        for finding in plan.findings
    ])
    if plan.status != "READY":
        report.status = "REJECTED"
        return report

    report.patches = [
        {
            "symbol": change.symbol,
            "index": change.index,
            "value": change.new_engineering,
            "reason": (f"кривая датчика: {curve.sensor} "
                       f"({curve.provenance}, источник: {curve.source})"),
            "evidence": f"documented_curve:{curve.provenance}",
            "requires_validation": True,
            "validation_hint": (
                "log MAF voltage, load and lambda at several points and compare with "
                "the supplier curve; a wrong curve shows up first in the trims"
            ),
            "planned": {
                "old_raw": change.old_raw,
                "new_raw": change.new_raw,
                "old_engineering": change.old_engineering,
                "new_engineering": change.new_engineering,
                "address": f"0x{change.address:04X}",
                "units": change.units,
            },
        }
        for change in plan.changes
        if change.new_raw != change.old_raw
    ]
    if not report.patches:
        report.status = "EMPTY"
        report.findings.append(_finding(
            "CURVE_ALREADY_MATCHES", "info",
            "the resampled curve is byte-identical to the one already in the binary; "
            "nothing will be written",
        ))
    return report


def dump_stock(stock_path: Path, symbol: str = MAF_TITLE) -> dict[str, Any]:
    """Export the stock curve in the curve-file format, grid included."""
    objects, _ = load_definition()
    obj = get_object(objects, symbol)
    grid = obj.axis_labels("y")
    image, offset, _ = load_calibration(stock_path)
    values = read_engineering(bytes(image[offset:offset + 0x6000]), obj)
    return {
        "sensor": f"stock MS41 calibration curve from {stock_path.name}",
        "source": str(stock_path),
        "provenance": "derived",
        "units": obj.units or "kg/h",
        "notes": ("Decompiled from the stock binary on the XDF voltage grid; not a "
                  "datasheet. Every column is listed: collapsing flat runs would turn "
                  "the stock steps into ramps and resampling would no longer reproduce "
                  "the table."),
        "points": [[round(grid[i], 4), round(values[i], 6)]
                   for i in range(obj.element_count)],
    }


def run(args: argparse.Namespace) -> int:
    policy = Policy.load(args.policy)
    if args.dump_stock:
        payload = dump_stock(args.stock, args.symbol)
        text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        if args.dump_stock == Path("-"):
            print(text, end="")
        else:
            args.dump_stock.write_text(text, encoding="utf-8")
            print(f"stock curve written to {args.dump_stock} "
                  f"({len(payload['points'])} points)")
        return 0

    if not args.curve:
        raise SystemExit("error: --curve is required unless --dump-stock is used")
    try:
        curve = load_curve(args.curve)
        report = build_report(args.stock, curve, policy, args.symbol)
    except (CurveError, PatchError) as exc:
        raise SystemExit(f"error: {exc}")

    payload = {
        "version": 1,
        "status": report.status,
        "generator": "MS41/tools/maf_curve.py",
        "source_sha256": report.source_sha256,
        "curve": report.curve.to_json() if report.curve else None,
        "patches": report.patches,
        "unfulfilled": [] if report.status == "READY" else [
            {"id": "maf_curve", "status": report.status,
             "detail": "the curve was refused; see the report"}
        ],
    }
    if args.out and report.status == "READY" and report.patches:
        args.out.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                            encoding="utf-8")
    if args.report:
        args.report.write_text(json.dumps(report.to_json(), indent=2, ensure_ascii=False)
                               + "\n", encoding="utf-8")
    if args.json:
        print(json.dumps({"report": report.to_json(), "patch_file": payload},
                         indent=2, ensure_ascii=False))
        return 1 if report.status == "REJECTED" else 0

    print(f"{report.status} {args.stock} <- {args.curve}")
    details = report.details.get("deviation_vs_stock", {})
    print(f"  grid: {report.details.get('columns')} columns, "
          f"0 ... {report.details.get('grid_full_scale_volts')} V, step "
          f"{report.details.get('grid_step_volts')} V")
    if details:
        print(f"  stock ceiling {details.get('stock_ceiling_kgh')} kg/h -> "
              f"{details.get('new_ceiling_kgh')} kg/h; cells changed "
              f"{details.get('cells_changed')}; max deviation "
              f"{details.get('max_absolute_kgh')} kg/h "
              f"({details.get('max_relative_percent')}%) at "
              f"{details.get('max_absolute_voltage')} V")
    for finding in report.findings:
        print(f"  [{finding['severity'].upper()}] {finding['code']}: {finding['message']}")
    if report.patches:
        print(f"  {len(report.patches)} columns planned; patch file: "
              f"{args.out or '(not written, use --out)'}")
    elif report.status == "EMPTY":
        print("  nothing to write: the curve already matches this binary")
    return 1 if report.status == "REJECTED" else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Resample a documented MAF curve onto the MS41 voltage grid.")
    parser.add_argument("--stock", type=Path, required=True,
                        help="stock binary to measure the current curve against")
    parser.add_argument("--curve", type=Path, default=None,
                        help="documented curve file (JSON)")
    parser.add_argument("--dump-stock", type=Path, default=None,
                        help="write the stock curve in curve-file format and exit "
                             "(use - for stdout)")
    parser.add_argument("--symbol", default=MAF_TITLE,
                        help=f"MAF table symbol (default: {MAF_TITLE})")
    parser.add_argument("--out", type=Path, default=None,
                        help="write a patch file for calibration.py here")
    parser.add_argument("--report", type=Path, default=None)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--json", action="store_true")
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
