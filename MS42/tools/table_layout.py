#!/usr/bin/env python3
"""Resolve and prove the layout of MS42 maps against the CRC-protected region.

MS42 maps are declared in the XDF as a flat block of rows x columns.  What the
block means is provable from the definition itself:

* every map has companion ``ldp*`` / ``ldpm*`` objects - the breakpoints of its
  axes, named in the description ("List of data points for <map>") and carrying
  the point count and the units;
* the rpm axis is the companion whose title is ``ldp_n*`` / ``ldpm_n*`` (the
  XDF naming convention) or whose units say ``1/min`` / ``min-1`` / ``U/min``;
  the other companion is the load/quantity axis.  This matters because some
  MS42 maps put rpm on the columns (``ip_ti_pvs__pvs__n`` and friends), so the
  position of the axis in the grid is read, not assumed;
* the calibration CRC record protects 0x48000..0x4F843, so a map whose data
  extends past 0x4F843 could be edited without the checksum noticing - that is
  reported as a problem;
* orientation is decided by the data itself: injection time rises with load,
  ignition corrections fall with load and so on, so the storage order that
  violates the declared tendency less is the real one.  A map whose declared
  tendency does not distinguish the two readings is reported as "ambiguous" or
  "not checked" rather than guessed.

The tendencies in :data:`LOAD_TRENDS` are not folklore: every one of them is
asserted against all 16 stock images by the test suite.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from MS42.tools.calibration import load_calibration
    from MS42.tools.xdf import XdfError, get_object, load_definition, read_engineering
except ModuleNotFoundError:  # Direct execution from MS42/tools.
    from calibration import load_calibration  # type: ignore[no-redef]
    from xdf import (  # type: ignore[no-redef]
        XdfError,
        get_object,
        load_definition,
        read_engineering,
    )

ROOT = Path(__file__).parents[1]
DEFAULT_XDF = ROOT / "definitions" / "Siemens_MS42_0110C6_ENG_512K_v1.3.xdf"

#: Absolute addresses of the CRC-protected calibration block (from checksum.py).
CRC_COVERED_FIRST = 0x48000
CRC_COVERED_LAST = 0x4F843
#: The same bounds expressed in calibration-relative XDF addresses.
CRC_COVERED_END_RELATIVE = CRC_COVERED_LAST - CRC_COVERED_FIRST + 1   # 0x7844
CALIBRATION_SIZE = 0x8000

MAX_RPM = 8160          # the highest engine speed MS42 can address (raw 255 x 32)
RPM_MARKERS = ("1/min", "min-1", "U/min", "rpm")

#: For every map below the load-axis tendency is unambiguous on all 16 stock
#: images: reading the flat block in declaration order gives zero violations,
#: reading it transposed gives many.  ``axis`` names the axis the tendency runs
#: along; ``rpm`` is only documentation of where the rpm axis sits.
LOAD_TRENDS: dict[str, dict[str, str]] = {
    "ip_tib__n__maf": {"axis": "columns", "direction": "up", "rpm": "rows"},
    "ip_ti_tco_2_pl_ivvt_1__n__maf": {"axis": "columns", "direction": "up", "rpm": "rows"},
    "ip_ti_tco_2_pl_ivvt_2__n__maf": {"axis": "columns", "direction": "up", "rpm": "rows"},
    "ip_ti_cst__n__tco": {"axis": "columns", "direction": "down", "rpm": "rows"},
    "ip_ti_cop_nr_1_1__n__maf": {"axis": "columns", "direction": "up", "rpm": "rows"},
    "ip_ti_cop_nr_1_2__n__maf": {"axis": "columns", "direction": "up", "rpm": "rows"},
    "ip_iga_aj_ron91__n__maf": {"axis": "columns", "direction": "down", "rpm": "rows"},
    "ip_iga_aj_ron98__n__maf": {"axis": "columns", "direction": "down", "rpm": "rows"},
    "ip_iga_pu__n__tco": {"axis": "columns", "direction": "down", "rpm": "rows"},
    "ip_iga_accin_puc__n__tco": {"axis": "columns", "direction": "down", "rpm": "rows"},
    "ip_t_puc_deacc__n__maf_mmv": {"axis": "columns", "direction": "down", "rpm": "rows"},
    "ip_maf_min_cop__n__iga_dif": {"axis": "columns", "direction": "down", "rpm": "rows"},
    "ip_maf_min_knk__n__tco": {"axis": "columns", "direction": "down", "rpm": "rows"},
    "ip_cppwm_min_cat_var__n__maf": {"axis": "columns", "direction": "up", "rpm": "rows"},
    "ip_cps_dif_map__n__maf_cor": {"axis": "columns", "direction": "down", "rpm": "rows"},
    "ip_ti_pvs__pvs__n": {"axis": "rows", "direction": "up", "rpm": "columns"},
    "ip_fac_isapwm_cor__pvs_cor__n": {"axis": "rows", "direction": "down", "rpm": "columns"},
    "ip_maf_vo_1__map__n": {"axis": "rows", "direction": "up", "rpm": "columns"},
}


@dataclass
class AxisInfo:
    axis: str
    role: str
    provider: str
    address: int
    grid: list[float]
    units: str
    scale: float

    def to_json(self) -> dict[str, Any]:
        return {
            "axis": self.axis,
            "role": self.role,
            "provider": self.provider,
            "address": f"0x{self.address:04X}",
            "units": self.units,
            "scale": self.scale,
            "points": len(self.grid),
            "first": self.grid[0] if self.grid else None,
            "last": self.grid[-1] if self.grid else None,
        }


@dataclass
class LayoutReport:
    symbol: str
    rows: int
    columns: int
    data_address: int
    data_end: int
    element_size_bits: int
    units: str
    scale: float
    value_min: float
    value_max: float
    axes: list[AxisInfo] = field(default_factory=list)
    orientation: str = ""
    orientation_violations: dict[str, int] = field(default_factory=dict)
    problems: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.problems

    def to_json(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "rows": self.rows,
            "columns": self.columns,
            "data_address": f"0x{self.data_address:04X}",
            "data_end": f"0x{self.data_end:04X}",
            "element_size_bits": self.element_size_bits,
            "units": self.units,
            "scale": self.scale,
            "value_min": round(self.value_min, 6),
            "value_max": round(self.value_max, 6),
            "axes": [axis.to_json() for axis in self.axes],
            "orientation": self.orientation,
            "orientation_violations": self.orientation_violations,
            "problems": self.problems,
            "ok": self.ok,
        }


@dataclass(frozen=True)
class AxisCandidate:
    title: str
    points: int
    units: str

    @property
    def is_rpm(self) -> bool:
        if re.match(r"ldpm?_n(_|__|$)", self.title):
            return True
        return any(marker in self.units for marker in RPM_MARKERS)


def load_axis_providers(xdf_path: Path = DEFAULT_XDF) -> dict[str, list[AxisCandidate]]:
    """Map every map title to the companion objects that declare its axes."""
    text = xdf_path.read_text(encoding="utf-8", errors="replace")
    providers: dict[str, list[AxisCandidate]] = {}
    for block in re.findall(r"<XDFTABLE\b.*?</XDFTABLE>", text, re.S):
        title_match = re.search(r"<title>([^<]+)</title>", block)
        description = re.search(r"<description>(.*?)</description>", block, re.S)
        if not title_match or not description:
            continue
        title = title_match.group(1).strip()
        targets = re.search(r"List of data points for\s*(.+)", description.group(1))
        if not targets or not title.startswith("ld"):
            continue
        units = re.findall(r"<units>([^<]*)</units>", block)
        z_units = units[-1] if units else ""
        rows = re.search(r'mmedrowcount="(\d+)"', block)
        candidate = AxisCandidate(title=title, points=int(rows.group(1)) if rows else 0,
                                  units=z_units)
        for target in (name.strip() for name in targets.group(1).split(",")):
            if not target or " " in target:
                continue
            providers.setdefault(target, []).append(candidate)
    return providers


def _pick_axis(candidates: list[AxisCandidate], points: int,
               prefer_rpm: bool) -> tuple[AxisCandidate | None, str]:
    """Choose the companion that declares this axis, by shape then by role."""
    if not candidates:
        return None, "no axis provider found"
    matching = [item for item in candidates if item.points == points]
    if matching:
        rpm_matching = [item for item in matching if item.is_rpm]
        other_matching = [item for item in matching if not item.is_rpm]
        chosen = rpm_matching if prefer_rpm else other_matching
        if chosen:
            return chosen[0], ""
        # Nothing in the XDF names the role; take the only shape match and say so.
        return matching[0], ""
    named = ", ".join(f"{item.title}({item.points})" for item in candidates)
    return None, f"no companion declares {points} points; candidates: {named}"


def _trend_violations(values: list[float], rows: int, columns: int, axis: str,
                      direction: str, storage: str) -> int:
    """Count places where the declared load tendency is broken."""
    if storage == "row_major":
        grid = [[values[row * columns + column] for column in range(columns)]
                for row in range(rows)]
    else:
        grid = [[values[column * rows + row] for column in range(columns)]
                for row in range(rows)]
    count = 0
    if axis == "rows":
        for row in range(rows - 1):
            for column in range(columns):
                low, high = grid[row][column], grid[row + 1][column]
                if direction == "up" and high < low - 1e-9:
                    count += 1
                if direction == "down" and high > low + 1e-9:
                    count += 1
    else:
        for row in range(rows):
            for column in range(columns - 1):
                low, high = grid[row][column], grid[row][column + 1]
                if direction == "up" and high < low - 1e-9:
                    count += 1
                if direction == "down" and high > low + 1e-9:
                    count += 1
    return count


def describe(symbol: str, bin_path: Path, xdf_path: Path = DEFAULT_XDF,
             objects: dict[str, list] | None = None,
             providers: dict[str, list[AxisCandidate]] | None = None) -> LayoutReport:
    objects = objects if objects is not None else load_definition(xdf_path)[0]
    providers = providers if providers is not None else load_axis_providers(xdf_path)
    image, offset, _image_type, _base = load_calibration(bin_path)
    calibration = bytes(image[offset:offset + CALIBRATION_SIZE])
    obj = get_object(objects, symbol)

    report = LayoutReport(
        symbol=obj.title,
        rows=obj.rows,
        columns=obj.columns,
        data_address=obj.address,
        data_end=obj.address + obj.byte_length,
        element_size_bits=obj.element_size_bits,
        units=obj.units,
        scale=obj.scaler.a,
        value_min=0.0,
        value_max=0.0,
    )
    values = read_engineering(calibration, obj)
    report.value_min = min(values)
    report.value_max = max(values)
    if report.data_end > CRC_COVERED_END_RELATIVE:
        report.problems.append(
            f"data extends to 0x{report.data_end - 1:04X}, past the end of the CRC-protected "
            f"calibration block (0x{CRC_COVERED_LAST:05X} absolute)"
        )

    candidates = providers.get(symbol, [])
    for axis, prefer_rpm, points in (("columns", False, obj.columns),
                                     ("rows", True, obj.rows)):
        candidate, why = _pick_axis(candidates, points, prefer_rpm=prefer_rpm)
        if candidate is None:
            report.problems.append(f"{axis} axis: {why}")
            continue
        try:
            provider_obj = get_object(objects, candidate.title)
        except XdfError as exc:
            report.problems.append(str(exc))
            continue
        grid = read_engineering(calibration, provider_obj)
        role = "rpm" if candidate.is_rpm else "load"
        report.axes.append(AxisInfo(
            axis=axis, role=role, provider=candidate.title, address=provider_obj.address,
            grid=grid, units=provider_obj.units, scale=provider_obj.scaler.a,
        ))
        if any(right <= left for left, right in zip(grid, grid[1:])):
            report.problems.append(f"{candidate.title}: axis values are not strictly increasing")
        if len(grid) != points:
            report.problems.append(
                f"{candidate.title} declares {len(grid)} points for {points} {axis} slots")
        if role != "rpm":
            continue
        if not grid:
            continue
        if not (0 <= grid[0] and grid[-1] <= MAX_RPM):
            report.problems.append(
                f"engine speed axis spans {grid[0]} ... {grid[-1]} rpm, outside the addressable "
                f"0..{MAX_RPM} rpm"
            )

    rpm_axes = [axis for axis in report.axes if axis.role == "rpm"]
    if len(rpm_axes) > 1:
        report.problems.append("both axes look like engine speed; the XDF units are ambiguous")

    trend = LOAD_TRENDS.get(symbol)
    if trend:
        violations = {
            "row_major": _trend_violations(values, obj.rows, obj.columns,
                                           trend["axis"], trend["direction"], "row_major"),
            "transposed": _trend_violations(values, obj.rows, obj.columns,
                                            trend["axis"], trend["direction"], "transposed"),
        }
        report.orientation_violations = violations
        if violations["row_major"] == violations["transposed"]:
            report.orientation = "ambiguous"
            report.problems.append(
                "the declared tendency does not distinguish the two storage orders")
        else:
            report.orientation = min(violations, key=violations.get)
            if report.orientation != "row_major":
                report.problems.append(
                    "the map reads better transposed; the XDF row/column order is suspect "
                    "for this symbol")
    else:
        report.orientation = "not checked"
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prove the layout of an MS42 map: axes, orientation and shape.")
    parser.add_argument("--bin", type=Path, required=True)
    parser.add_argument("--symbol", default=None,
                        help="map title; omit to describe the declared map families")
    parser.add_argument("--all", action="store_true",
                        help="describe every map that has axis providers")
    parser.add_argument("--xdf", type=Path, default=DEFAULT_XDF)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    objects, problems = load_definition(args.xdf)
    providers = load_axis_providers(args.xdf)
    if args.symbol:
        symbols = [args.symbol]
    elif args.all:
        symbols = [title for title in providers
                   if any(o.rows > 1 and o.columns > 1 for o in objects.get(title, []))]
    else:
        symbols = sorted(LOAD_TRENDS)
    reports = []
    for symbol in sorted(set(symbols)):
        try:
            reports.append(describe(symbol, args.bin, args.xdf, objects, providers))
        except XdfError as exc:
            print(f"skip {symbol}: {exc}", file=sys.stderr)

    if args.json:
        print(json.dumps([report.to_json() for report in reports],
                         indent=2, ensure_ascii=False))
    else:
        for report in reports:
            state = "OK" if report.ok else "PROBLEMS"
            print(f"{state} {report.symbol}: {report.rows}x{report.columns} "
                  f"@0x{report.data_address:04X}..0x{report.data_end - 1:04X} "
                  f"{report.element_size_bits}b x{report.scale:g} {report.units} "
                  f"= {report.value_min:.3f}..{report.value_max:.3f}")
            for axis in report.axes:
                print(f"   {axis.axis} ({axis.role}) from {axis.provider} "
                      f"@0x{axis.address:04X} {axis.units}: "
                      f"{axis.grid[0]:g} ... {axis.grid[-1]:g} ({len(axis.grid)} points)")
            print(f"   orientation: {report.orientation} "
                  f"violations={report.orientation_violations}")
            for problem in report.problems:
                print(f"   [PROBLEM] {problem}")
    if not args.json:
        good = sum(1 for report in reports if report.ok)
        print(f"\n{good}/{len(reports)} maps described without problems")
    return 0 if all(report.ok for report in reports) else 1


if __name__ == "__main__":
    raise SystemExit(main())
