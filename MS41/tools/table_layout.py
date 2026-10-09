#!/usr/bin/env python3
"""Resolve and prove the layout of MS41 maps.

Every 16x12 map in the XDF carries its axes as *linked* definitions: the axis
block holds an address, and the breakpoint values live at that address in the
binary. The companion ``ldp*`` / ``ldpm*`` objects are the same arrays declared
as tables ("List of data points for <map>, <map>, ..."), which is what makes the
grid readable: they carry the scaling the linked axis block lacks.

So a map's layout is provable without logs:

* rows and columns come from the XDF z axis, and the flat data must lie in one
  contiguous run inside the CRC-protected region;
* the axis values must be strictly increasing and in physical ranges;
* the orientation is decided by the data itself: air mass and injection time
  rise with load and fall with engine speed, so the correct reading has almost
  no violations and the transposed one has many.

Output is a description plus a list of problems, never a silent assumption.
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
    from MS41.tools.calibration import load_calibration
    from MS41.tools.xdf import (
        XdfError,
        get_object,
        load_definition,
        read_engineering,
    )
except ModuleNotFoundError:  # Direct execution from MS41/tools.
    from calibration import load_calibration
    from xdf import XdfError, get_object, load_definition, read_engineering

DEFAULT_XDF = Path(__file__).parents[1] / "definitions" / "Siemens_MS41_1429861_ENG_24K_v1.0.xdf"

#: Which physical direction each axis is expected to have.
RPM_DIRECTION = "rpm"
LOAD_DIRECTION = "load"

#: Trends used to tell the two readings of a map apart. They are tendencies, not
#: laws: the ignition base map advances with load again at high engine speed, and
#: the VANOS maps are corrections that oscillate around zero. A tendency decides
#: the orientation (its reading must violate it less), nothing more.
#:   "up"   - values tend to rise as the axis rises
#:   "down" - values tend to fall as the axis rises
DECLARED_TRENDS: dict[str, dict[str, str]] = {
    "ip_tib__n__maf": {"load": "up", "rpm": "down"},
    "ip_igab__n__maf": {"load": "down", "rpm": "up"},
    "ip_iga_ivvt__n__maf": {"load": "down", "rpm": "up"},
}

#: Trends that hold as a law for the stock family, checked with a tolerance of a
#: few cells. Today only the base injection time qualifies: it rises with load in
#: every row but one across all 13 fixtures.
STRICT_TRENDS: dict[str, dict[str, str]] = {
    "ip_tib__n__maf": {"load": "up"},
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


def load_axis_providers(xdf_path: Path = DEFAULT_XDF) -> dict[str, list[AxisCandidate]]:
    """Map every map title to the companion objects that declare its axes.

    The XDF spells this out in plain text: ``List of data points for A, B, C``.
    Each candidate is a real object with an address, a point count and units, so
    the axis assignment is decided by the map's own shape and units rather than by
    the order in which the companions appear.
    """
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
    """Choose the companion that declares this axis, by shape then by units."""
    if not candidates:
        return None, "no axis provider found"
    matching = [item for item in candidates if item.points == points]
    if matching:
        rpm_named = [item for item in matching if "rpm" in item.units]
        chosen = (rpm_named or matching) if prefer_rpm else (
            [item for item in matching if "rpm" not in item.units] or matching)
        return chosen[0], ""
    return None, (f"no companion declares {points} points; candidates: "
                  + ", ".join(f"{item.title}({item.points})" for item in candidates))


def _violations(values: list[float], rows: int, columns: int, orientation: str,
                expectation: dict[str, str]) -> int:
    """Count places where a declared expectation is broken.

    Row = engine speed, column = load. ``expectation`` says whether the values are
    expected to rise or fall along each axis; the transposed reading swaps the
    roles, so a wrong orientation shows up as many more violations.
    """
    if orientation == "rpm_major":
        grid = [[values[row * columns + column] for column in range(columns)]
                for row in range(rows)]
    else:
        # Same flat data read the other way round: row index is engine speed, so
        # the load index varies fastest in the binary.
        grid = [[values[column * rows + row] for column in range(columns)]
                for row in range(rows)]
    count = 0
    load_direction = expectation.get("load")
    rpm_direction = expectation.get("rpm")
    for row in grid:
        for left, right in zip(row, row[1:]):
            if load_direction == "up" and right < left - 1e-9:
                count += 1
            if load_direction == "down" and right > left + 1e-9:
                count += 1
    for column in range(len(grid[0]) if grid else 0):
        series = [row[column] for row in grid]
        for high, low in zip(series, series[1:]):
            if rpm_direction == "up" and low < high - 1e-9:
                count += 1
            if rpm_direction == "down" and low > high + 1e-9:
                count += 1
    return count


def describe(symbol: str, bin_path: Path, xdf_path: Path = DEFAULT_XDF,
             objects: dict[str, list] | None = None,
             providers: dict[str, dict[str, str]] | None = None) -> LayoutReport:
    objects = objects if objects is not None else load_definition(xdf_path)[0]
    providers = providers if providers is not None else load_axis_providers(xdf_path)
    image, offset, _ = load_calibration(bin_path)
    calibration = bytes(image[offset:offset + 0x6000])
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
    if report.data_end > 0x2D4A:
        report.problems.append(
            f"data extends to 0x{report.data_end:04X}, past the CRC-protected region")

    candidates = providers.get(symbol, [])
    for axis, role in (("x", LOAD_DIRECTION), ("y", RPM_DIRECTION)):
        points = obj.columns if axis == "x" else obj.rows
        candidate, why = _pick_axis(candidates, points, prefer_rpm=(axis == "y"))
        if candidate is None:
            report.problems.append(f"{axis} axis: {why}")
            continue
        provider = candidate.title
        try:
            provider_obj = get_object(objects, provider)
        except XdfError as exc:
            report.problems.append(str(exc))
            continue
        grid = read_engineering(calibration, provider_obj)
        report.axes.append(AxisInfo(
            axis=axis, role=role, provider=provider, address=provider_obj.address,
            grid=grid, units=provider_obj.units, scale=provider_obj.scaler.a,
        ))
        if any(b <= a for a, b in zip(grid, grid[1:])):
            report.problems.append(f"{provider}: axis values are not strictly increasing")
        expected = obj.columns if axis == "x" else obj.rows
        if len(grid) != expected:
            report.problems.append(
                f"{provider} declares {len(grid)} points for {expected} {axis} slots")

    rpm_axis = next((axis for axis in report.axes if axis.role == RPM_DIRECTION), None)
    load_axis = next((axis for axis in report.axes if axis.role == LOAD_DIRECTION), None)
    if rpm_axis:
        if not (400 <= rpm_axis.grid[0] and rpm_axis.grid[-1] <= 8000):
            report.problems.append(
                f"engine speed axis spans {rpm_axis.grid[0]} ... {rpm_axis.grid[-1]} rpm, "
                "outside any MS41 range")
        if rpm_axis.units != "rpm":
            report.problems.append(f"engine speed axis is declared in {rpm_axis.units!r}")
    if load_axis and not load_axis.units:
        report.problems.append("load axis has no units")

    expectation = DECLARED_TRENDS.get(symbol)
    if expectation:
        violations = {
            "rpm_major": _violations(values, obj.rows, obj.columns, "rpm_major",
                                     expectation),
            "transposed": _violations(values, obj.rows, obj.columns, "transposed",
                                      expectation),
        }
        report.orientation_violations = violations
        if violations["rpm_major"] == violations["transposed"]:
            report.orientation = "ambiguous"
            report.problems.append(
                "the declared behaviour does not distinguish the two orientations")
        else:
            report.orientation = min(violations, key=violations.get)
            if report.orientation != "rpm_major":
                report.problems.append(
                    "the map reads better transposed; the XDF row/column order is "
                    "suspect for this symbol")
    strict = STRICT_TRENDS.get(symbol)
    if strict:
        broken = _violations(values, obj.rows, obj.columns, report.orientation
                             if report.orientation in ("rpm_major", "transposed")
                             else "rpm_major", strict)
        tolerance = max(2, obj.element_count // 20)
        if broken > tolerance:
            report.problems.append(
                f"{broken} places break the strict trend {strict} "
                f"(tolerance {tolerance})")
    if not expectation:
        report.orientation = "not checked"
    return report


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Prove the layout of an MS41 map: axes, orientation and shape.")
    parser.add_argument("--bin", type=Path, required=True)
    parser.add_argument("--symbol", default=None,
                        help="map title; omit to describe the declared map families")
    parser.add_argument("--all", action="store_true",
                        help="describe every map that has an axis provider")
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
        symbols = sorted(DECLARED_TRENDS)
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
                  f"@0x{report.data_address:04X}..0x{report.data_end:04X} "
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
