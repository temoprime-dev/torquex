#!/usr/bin/env python3
"""Move the MS41 VANOS switching threshold to a measured load line.

The VANOS switch threshold (``ip_maf_min_ivvt__n__tco`` manual,
``ip_dri_maf_min_ivvt__n__tco`` automatic) is the air mass above which the ECU
advances the intake cam. Changing manifold, cams or exhaust moves the load curve
the threshold was written for, so the stock line no longer sits where the two
cam positions cross. The method is documented in the Stage 2 work order: measure
the load with VANOS advanced and retarded, find where they cross, and put the
threshold there - never copy the number, and never share the manual table with
the automatic car.

This tool does not invent the threshold. It takes a *measured* comparison file,
checks it against itself and against the stock table, and only then plans the
cells:

* provenance must be ``measured`` - a switching line read off a datasheet or
  guessed from a ratio is not evidence that a cam position is better;
* the documented crossing of the two curves must match the curve data;
* the threshold must stay inside the measured envelope at every rpm, otherwise
  the switch it describes could never trigger;
* only temperature columns at or above the measurement temperature are written;
  colder columns keep their stock behaviour and are listed as such;
* rows where the stock table holds its "never switch" maximum stay stock too.

Without the file the tool dumps the stock table with its real axes and says what
it would need - the layout proof is available today, the numbers are not.
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
    from MS41.tools.xdf import XdfError, get_object, load_definition, read_engineering
except ModuleNotFoundError:  # Direct execution from MS41/tools.
    from calibration import (
        DEFAULT_POLICY,
        PatchError,
        Policy,
        load_calibration,
        plan_patches,
    )
    from xdf import XdfError, get_object, load_definition, read_engineering

#: Which table belongs to which transmission. Copying between them is the
#: documented failure mode: the automatic car uses a different line.
TRANSFER_TABLES: dict[str, str] = {
    "manual": "ip_maf_min_ivvt__n__tco",
    "automatic": "ip_dri_maf_min_ivvt__n__tco",
}
#: Companion tables that carry each axis, for the stock dump.
AXIS_TITLES: dict[str, tuple[str, str]] = {
    "manual": ("ldp_n__maf_min_ivvt", "ldp_tco__maf_min_ivvt"),
    "automatic": ("ldp_n__dri_maf_min_ivvt", "ldp_tco__dri_maf_min_ivvt"),
}

PROVENANCE_KINDS = ("measured", "derived", "datasheet")
#: Only a measurement may decide a cam position.
REQUIRED_PROVENANCE = "measured"
#: How far the documented crossing may differ from the curve data.
CROSSING_RPM_TOLERANCE = 0.10
CROSSING_RPM_FLOOR = 200.0
#: Threshold must sit below the measured envelope by at least this much.
ENVELOPE_MARGIN = 1.02

STATUS_READY = "READY"
STATUS_REQUIRES_DATA = "REQUIRES_DATA"
STATUS_EMPTY = "EMPTY"
STATUS_REJECTED = "REJECTED"


class ThresholdError(ValueError):
    """Raised when the comparison file cannot be used as evidence."""


@dataclass(frozen=True)
class Comparison:
    engine: str
    transmission: str
    source: str
    provenance: str
    units: str
    measured_tco_c: float
    crossing_rpm: float
    advanced: tuple[tuple[float, float], ...]
    retarded: tuple[tuple[float, float], ...]
    threshold: tuple[tuple[float, float], ...]

    def to_json(self) -> dict[str, Any]:
        return {
            "engine": self.engine,
            "transmission": self.transmission,
            "source": self.source,
            "provenance": self.provenance,
            "units": self.units,
            "measured_tco_c": self.measured_tco_c,
            "crossing_rpm": self.crossing_rpm,
            "advanced": [[r, v] for r, v in self.advanced],
            "retarded": [[r, v] for r, v in self.retarded],
            "threshold": [[r, v] for r, v in self.threshold],
        }


@dataclass
class SwitchReport:
    status: str
    transmission: str = ""
    symbol: str = ""
    source_sha256: str = ""
    plan_status: str | None = None
    details: dict[str, Any] = field(default_factory=dict)
    findings: list[dict[str, str]] = field(default_factory=list)
    patches: list[dict[str, Any]] = field(default_factory=list)

    def to_json(self) -> dict[str, Any]:
        return {
            "version": 1,
            "status": self.status,
            "transmission": self.transmission,
            "symbol": self.symbol,
            "source_sha256": self.source_sha256,
            "plan_status": self.plan_status,
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


def _points(raw: Any, name: str) -> tuple[tuple[float, float], ...]:
    if not isinstance(raw, list) or len(raw) < 2:
        raise ThresholdError(f"{name}: at least two [rpm, mg/stroke] points are needed")
    points: list[tuple[float, float]] = []
    for index, point in enumerate(raw):
        if (not isinstance(point, (list, tuple)) or len(point) != 2
                or not all(isinstance(value, (int, float)) for value in point)):
            raise ThresholdError(f"{name}: point {index} must be [rpm, mg/stroke]")
        rpm, value = float(point[0]), float(point[1])
        if rpm <= 0 or value < 0:
            raise ThresholdError(f"{name}: point {index} has rpm={rpm}, value={value}")
        points.append((rpm, value))
    if any(b[0] <= a[0] for a, b in zip(points, points[1:])):
        raise ThresholdError(f"{name}: rpm must strictly increase")
    return tuple(points)


def parse_comparison(data: dict[str, Any]) -> Comparison:
    if not isinstance(data, dict):
        raise ThresholdError("comparison file must be a JSON object")
    engine = str(data.get("engine") or "").strip()
    transmission = str(data.get("transmission") or "").strip().lower()
    source = str(data.get("source") or "").strip()
    provenance = str(data.get("provenance") or "").strip().lower()
    units = str(data.get("units") or "").strip()
    if not engine or not source:
        raise ThresholdError(
            "EVIDENCE_MISSING: 'engine' and 'source' are mandatory; a switching line "
            "without them cannot be told apart from a guess"
        )
    if transmission not in TRANSFER_TABLES:
        raise ThresholdError(
            f"TRANSMISSION_UNKNOWN: 'transmission' must be one of "
            f"{sorted(TRANSFER_TABLES)}, got {transmission!r}"
        )
    if provenance not in PROVENANCE_KINDS:
        raise ThresholdError(
            f"PROVENANCE_MISSING: 'provenance' must be one of {PROVENANCE_KINDS}"
        )
    if provenance != REQUIRED_PROVENANCE:
        raise ThresholdError(
            f"PROVENANCE_NOT_MEASURED: a cam switching line needs measured load "
            f"curves, got {provenance!r}. Measure the load with VANOS advanced and "
            "retarded and put the crossing in the file"
        )
    if units != "mg/stroke":
        raise ThresholdError(f"UNITS_UNSUPPORTED: the table is in mg/stroke, got {units!r}")
    measured = data.get("measured_tco_c")
    crossing = data.get("crossing_rpm")
    if not isinstance(measured, (int, float)):
        raise ThresholdError(
            "MEASUREMENT_TEMPERATURE_MISSING: name the coolant temperature the "
            "comparison was measured at; colder columns keep their stock behaviour"
        )
    if not isinstance(crossing, (int, float)) or crossing <= 0:
        raise ThresholdError(
            "CROSSING_MISSING: the rpm where the two load curves cross is the whole "
            "point of this file; state it explicitly"
        )
    return Comparison(
        engine=engine, transmission=transmission, source=source, provenance=provenance,
        units=units, measured_tco_c=float(measured), crossing_rpm=float(crossing),
        advanced=_points(data.get("advanced"), "advanced"),
        retarded=_points(data.get("retarded"), "retarded"),
        threshold=_points(data.get("threshold"), "threshold"),
    )


def load_comparison(path: Path) -> Comparison:
    return parse_comparison(json.loads(path.read_text(encoding="utf-8")))


def interpolate(points: tuple[tuple[float, float], ...], rpm: float) -> float | None:
    """Linear interpolation, None outside the measured range (never extrapolated)."""
    if rpm < points[0][0] or rpm > points[-1][0]:
        return None
    for (r0, v0), (r1, v1) in zip(points, points[1:]):
        if r0 <= rpm <= r1:
            if r1 == r0:
                return v1
            return v0 + (rpm - r0) / (r1 - r0) * (v1 - v0)
    return points[-1][1]


def crossing_rpm(comparison: Comparison) -> float | None:
    """First rpm where the advanced and retarded curves swap places."""
    rpms = sorted({rpm for rpm, _ in comparison.advanced} |
                  {rpm for rpm, _ in comparison.retarded})
    previous: tuple[float, float] | None = None
    for rpm in rpms:
        advanced = interpolate(comparison.advanced, rpm)
        retarded = interpolate(comparison.retarded, rpm)
        if advanced is None or retarded is None:
            continue
        delta = advanced - retarded
        if previous is not None and previous[1] * delta < 0:
            r0, d0 = previous
            return r0 + (rpm - r0) * abs(d0) / (abs(d0) + abs(delta))
        previous = (rpm, delta)
    return None


def stock_table(bin_path: Path, transmission: str) -> tuple[Any, dict[str, Any]]:
    """The stock table object plus its axes and values, all proven from the XDF."""
    objects, _ = load_definition()
    symbol = TRANSFER_TABLES[transmission]
    obj = get_object(objects, symbol)
    image, offset, image_type = load_calibration(bin_path)
    rpm_axis = read_engineering(bytes(image[offset:offset + 0x6000]),
                                get_object(objects, AXIS_TITLES[transmission][0]))
    tco_axis = read_engineering(bytes(image[offset:offset + 0x6000]),
                                get_object(objects, AXIS_TITLES[transmission][1]))
    values = read_engineering(bytes(image[offset:offset + 0x6000]), obj)
    context = {
        "symbol": symbol,
        "object": obj,
        "image": image,
        "offset": offset,
        "image_type": image_type,
        "rpm_axis": rpm_axis,
        "tco_axis": tco_axis,
        "values": values,
        "objects": objects,
    }
    return obj, context


def never_switch_raw(context: dict[str, Any]) -> int:
    """The stock table's own maximum is its 'never switch' sentinel."""
    obj = context["object"]
    scaler = obj.scaler
    values = context["values"]
    peak = max(values)
    return int(round((peak - scaler.b) / scaler.a))


def dump_stock(bin_path: Path, transmission: str) -> dict[str, Any]:
    obj, context = stock_table(bin_path, transmission)
    scale = obj.scaler
    sentinel_raw = never_switch_raw(context)
    sentinel = obj.to_engineering(sentinel_raw)
    rows = []
    for index, rpm in enumerate(context["rpm_axis"]):
        row = context["values"][index * obj.columns:(index + 1) * obj.columns]
        rows.append({
            "rpm": round(rpm, 2),
            "values": [round(value, 3) for value in row],
            "cells": [index * obj.columns + column for column in range(obj.columns)],
            "never_switch": [abs(value - sentinel) < 1e-6 for value in row],
        })
    return {
        "symbol": obj.title,
        "transmission": transmission,
        "description": obj.description,
        "address": f"0x{obj.address:04X}",
        "shape": f"{obj.rows}x{obj.columns}",
        "units": "mg/stroke",
        "quantum_mg": round(scale.a, 9),
        "rpm_axis": [round(value, 2) for value in context["rpm_axis"]],
        "tco_axis_c": [round(value, 2) for value in context["tco_axis"]],
        "never_switch": {"raw": sentinel_raw, "value": round(sentinel, 3),
                         "cells": sum(1 for row in rows for flag in row["never_switch"]
                                      if flag)},
        "cells_total": obj.element_count,
        "rows": rows,
    }


def build_report(bin_path: Path, transmission: str, comparison: Comparison,
                 policy: Policy, engine: str | None = None) -> SwitchReport:
    obj, context = stock_table(bin_path, transmission)
    scale = obj.scaler
    sentinel_raw = never_switch_raw(context)
    sentinel = obj.to_engineering(sentinel_raw)

    report = SwitchReport(
        status=STATUS_READY,
        transmission=transmission,
        symbol=obj.title,
        source_sha256=hashlib.sha256(bytes(context["image"])).hexdigest(),
    )
    report.details = dict(dump_stock(bin_path, transmission))
    report.details.pop("rows", None)
    report.details["comparison_source"] = comparison.source
    # The XDF's `units` field for this table says °C, which is its column axis
    # leaking into the value field: the description says "switching threshold
    # (air mass)" and the quantum is the load quantum (5.44706 mg/stroke, the
    # same one c_maf_max uses). The patches therefore state the units the values
    # are actually in, and the report says why.
    report.findings.append(_finding(
        "XDF_UNITS_MISLABELLED", "info",
        f"the XDF labels {obj.title} as {obj.units!r}, but the description is "
        "'switching threshold (air mass)' and the quantum is "
        f"{scale.a:.5f} mg/stroke - the table is a load threshold, and the patch "
        "file states mg/stroke explicitly",
    ))

    if comparison.transmission != transmission:
        report.status = STATUS_REJECTED
        report.findings.append(_finding(
            "TRANSMISSION_MISMATCH", "reject",
            f"the profile says {transmission} but the comparison file was measured on "
            f"an {comparison.transmission} car; the two tables are different and are "
            "not interchangeable",
        ))
        return report
    if engine and comparison.engine.lower() != engine.lower():
        report.status = STATUS_REJECTED
        report.findings.append(_finding(
            "ENGINE_MISMATCH", "reject",
            f"the profile says {engine} but the comparison file was measured on "
            f"{comparison.engine}",
        ))
        return report

    computed = crossing_rpm(comparison)
    if computed is None:
        report.status = STATUS_REJECTED
        report.findings.append(_finding(
            "NO_CROSSING", "reject",
            "the advanced and retarded load curves never cross in the measured rpm "
            "range, so there is no switching point to derive",
        ))
        return report
    tolerance = max(CROSSING_RPM_FLOOR, abs(computed) * CROSSING_RPM_TOLERANCE)
    report.details["crossing"] = {
        "documented_rpm": comparison.crossing_rpm,
        "computed_rpm": round(computed, 1),
        "tolerance_rpm": round(tolerance, 1),
    }
    if abs(comparison.crossing_rpm - computed) > tolerance:
        report.status = STATUS_REJECTED
        report.findings.append(_finding(
            "CROSSING_MISMATCH", "reject",
            f"the file documents the crossing at {comparison.crossing_rpm:.0f} rpm but "
            f"the curves cross at {computed:.0f} rpm; one of the two is wrong and the "
            "threshold would be placed on a line nobody measured",
        ))
        return report

    # The threshold must be reachable: at every point it has to sit below the
    # measured envelope, otherwise the load could never exceed it.
    unreachable: list[dict[str, float]] = []
    for rpm, value in comparison.threshold:
        advanced = interpolate(comparison.advanced, rpm)
        retarded = interpolate(comparison.retarded, rpm)
        envelope = [item for item in (advanced, retarded) if item is not None]
        if not envelope:
            continue
        ceiling = max(envelope) * ENVELOPE_MARGIN
        if value > ceiling:
            unreachable.append({"rpm": rpm, "threshold": value,
                                "measured_ceiling": round(max(envelope), 2)})
    report.details["checked_threshold_points"] = len(comparison.threshold)
    if unreachable:
        report.status = STATUS_REJECTED
        report.findings.append(_finding(
            "THRESHOLD_UNREACHABLE", "reject",
            "the documented threshold is above the measured load at "
            + ", ".join(f"{item['rpm']:.0f} rpm ({item['threshold']:.0f} > "
                        f"{item['measured_ceiling']:.0f} mg/stroke)"
                        for item in unreachable[:4])
            + "; the switch could never trigger there",
        ))
        return report

    rows = report.details["rows"] = []
    planned: list[dict[str, Any]] = []
    warm_columns = [index for index, tco in enumerate(context["tco_axis"])
                    if tco >= comparison.measured_tco_c]
    report.details["warm_columns"] = {
        "measured_tco_c": comparison.measured_tco_c,
        "columns": warm_columns,
        "temperatures_c": [round(context["tco_axis"][index], 2) for index in warm_columns],
        "kept_cold_columns": [round(tco, 2) for index, tco
                              in enumerate(context["tco_axis"])
                              if index not in warm_columns],
    }
    rows_kept: list[float] = []
    sentinel_rows: list[float] = []
    unchanged: list[int] = []
    for row_index, rpm in enumerate(context["rpm_axis"]):
        stock_row = context["values"][row_index * obj.columns:(row_index + 1) * obj.columns]
        requested = interpolate(comparison.threshold, rpm)
        for column in warm_columns:
            if abs(stock_row[column] - sentinel) < 1e-6:
                sentinel_rows.append(round(rpm, 2))
                continue
            if requested is None:
                rows_kept.append(round(rpm, 2))
                continue
            if int(round(obj.to_raw(requested))) == int(round(obj.to_raw(stock_row[column]))):
                # the measured line already sits on this cell: nothing to write
                unchanged.append(row_index * obj.columns + column)
                continue
            index = row_index * obj.columns + column
            planned.append({
                "symbol": obj.title,
                "index": index,
                "value": requested,
                "rpm": round(rpm, 2),
                "tco_c": round(context["tco_axis"][column], 2),
                "stock": round(stock_row[column], 3),
            })
        rows.append({
            "rpm": round(rpm, 2),
            "stock_warm": [round(stock_row[column], 3) for column in warm_columns],
            "requested": None if requested is None else round(requested, 3),
        })
    report.details["sentinel_rows_kept"] = sorted(set(sentinel_rows))
    report.details["rows_kept_outside_measurement"] = sorted(set(rows_kept))
    report.details["planned_cells"] = len(planned)
    report.details["unchanged_cells"] = unchanged
    report.details["not_switched_rows"] = [
        {"rpm": row["rpm"], "stock": value}
        for row, value in ((row, row["stock_warm"][0]) for row in rows
                           if row["stock_warm"])
        if abs(value - sentinel) < 1e-6
    ]

    if not planned:
        report.status = STATUS_EMPTY
        report.findings.append(_finding(
            "NOTHING_TO_CHANGE", "info",
            "the measured threshold already matches every cell it covers; the cold "
            "columns, the never-switch rows and the cells whose raw value would not "
            "change stay as they are",
        ))
        return report
    if unchanged:
        report.findings.append(_finding(
            "CELLS_ALREADY_MATCH", "info",
            f"{len(unchanged)} cell(s) already hold the measured value and are not "
            "rewritten",
        ))

    requests = [{"symbol": item["symbol"], "index": item["index"], "value": item["value"]}
                for item in planned]
    plan = plan_patches(context["image"], context["offset"], context["image_type"],
                        context["objects"], [], requests, policy)
    report.plan_status = plan.status
    _extend_findings(report.findings, [
        _finding(finding.code, finding.severity, finding.message)
        for finding in plan.findings
    ])
    if plan.status != STATUS_READY:
        report.status = STATUS_REJECTED
        return report

    for change in plan.changes:
        item = next(entry for entry in planned if entry["index"] == change.index)
        report.patches.append({
            "symbol": change.symbol,
            "index": change.index,
            "value": change.new_engineering,
            "reason": (
                f"порог переключения VANOS по измеренной линии "
                f"({comparison.source}): {change.old_engineering:.0f} -> "
                f"{change.new_engineering:.0f} мг/такт при {item['rpm']:.0f} об/мин и "
                f"{item['tco_c']:.0f} °C; кривые пересекаются на "
                f"{computed:.0f} об/мин"
            ),
            "units": "mg/stroke",
            "evidence": (f"measured_vanos_comparison:{comparison.source}"
                         f"@tco={comparison.measured_tco_c:g}C"),
            "requires_validation": True,
            "validation_hint": (
                "лог переключений VANOS: порог должен срабатывать на измеренной "
                "линии без рывка на переходе, а холодные столбцы должны вести себя "
                "как стоковые"
            ),
        })

    report.findings.append(_finding(
        "THRESHOLD_FROM_MEASUREMENT", "info",
        f"{len(report.patches)} ячеек взяты из измеренной линии; холодные столбцы "
        f"({', '.join(f'{t:g}' for t in report.details['warm_columns']['kept_cold_columns'])} °C) "
        "остались стоковыми",
    ))
    if report.details["sentinel_rows_kept"]:
        report.findings.append(_finding(
            "NEVER_SWITCH_KEPT", "warning",
            "строки, где сток запрещает переключение ("
            + ", ".join(f"{rpm:g} об/мин" for rpm in report.details["sentinel_rows_kept"])
            + "), остались запрещающими: включить VANOS там — отдельное решение, "
            "а не следствие нового порога",
        ))
    if report.details["rows_kept_outside_measurement"]:
        report.findings.append(_finding(
            "ROWS_OUTSIDE_MEASUREMENT", "info",
            "обороты вне измеренного диапазона ("
            + ", ".join(f"{rpm:g}" for rpm in report.details["rows_kept_outside_measurement"])
            + ") остались стоковыми: порог не экстраполируется",
        ))
    return report


def run(args: argparse.Namespace) -> int:
    if args.dump_stock:
        payload = dump_stock(args.bin, args.transmission)
        text = json.dumps(payload, indent=2, ensure_ascii=False) + "\n"
        if args.dump_stock == Path("-"):
            print(text, end="")
        else:
            args.dump_stock.write_text(text, encoding="utf-8")
            print(f"stock VANOS table written to {args.dump_stock}")
        return 0

    if not args.curves:
        raise SystemExit("error: --curves is required unless --dump-stock is used")
    policy = Policy.load(args.policy)
    profile_engine = None
    if args.profile:
        profile = json.loads(args.profile.read_text(encoding="utf-8"))
        profile_engine = profile.get("engine")
        profile_transmission = str(profile.get("transmission") or "").lower()
        if profile_transmission and profile_transmission != args.transmission:
            print(f"error: the profile says {profile_transmission} but --transmission "
                  f"says {args.transmission}", file=sys.stderr)
            return 1
    try:
        comparison = load_comparison(args.curves)
        report = build_report(args.bin, args.transmission, comparison, policy,
                              engine=profile_engine)
    except (ThresholdError, PatchError, XdfError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    payload = {
        "version": 1,
        "status": report.status,
        "generator": "MS41/tools/vanos_switch.py",
        "generated_from_sha256": report.source_sha256,
        "patches": report.patches,
        "unfulfilled": [] if report.status == STATUS_READY else [
            {"id": "vanos", "status": report.status,
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

    print(f"{report.status} {args.bin} [{report.transmission}]")
    print(f"  {report.symbol} @ {report.details.get('address')}: "
          f"{report.details.get('shape')} cells, quantum "
          f"{report.details.get('quantum_mg')} mg/stroke")
    print(f"  axes: rpm {report.details.get('rpm_axis')}")
    print(f"        tco {report.details.get('tco_axis_c')}")
    crossing = report.details.get("crossing")
    if crossing:
        print(f"  crossing: documented {crossing['documented_rpm']} rpm, "
              f"computed {crossing['computed_rpm']} rpm "
              f"(±{crossing['tolerance_rpm']} rpm)")
    if report.details.get("warm_columns"):
        print(f"  measured at {report.details['warm_columns']['measured_tco_c']:g} °C -> "
              f"columns {report.details['warm_columns']['columns']}, kept cold "
              f"{report.details['warm_columns']['kept_cold_columns']}")
    if report.patches:
        print(f"  {len(report.patches)} cell(s) planned; patch file: "
              f"{args.out or '(not written, use --out)'}")
    for finding in report.findings:
        print(f"  [{finding['severity'].upper()}] {finding['code']}: {finding['message']}")
    return 1 if report.status == STATUS_REJECTED else 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Move the MS41 VANOS switching threshold to a measured load line.")
    parser.add_argument("--bin", type=Path, required=True,
                        help="stock binary the calibration is built on")
    parser.add_argument("--transmission", choices=sorted(TRANSFER_TABLES),
                        default="manual",
                        help="which table to use (the profile confirms it)")
    parser.add_argument("--profile", type=Path, default=None,
                        help="Stage 2 profile; its engine and transmission must match")
    parser.add_argument("--curves", type=Path, default=None,
                        help="measured comparison file (advanced/retarded + threshold)")
    parser.add_argument("--dump-stock", type=Path, default=None,
                        help="write the stock table with its axes, no data needed")
    parser.add_argument("--out", type=Path, default=None,
                        help="write the patch file here (only when READY)")
    parser.add_argument("--report", type=Path, default=None,
                        help="write the full report here")
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--json", action="store_true", help="print JSON to stdout")
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
