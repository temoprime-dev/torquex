#!/usr/bin/env python3
"""Validate the MS41 24K/256K XDF base-offset relationship against BIN files."""

from __future__ import annotations

import argparse
import copy
import json
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path

PARTIAL_SIZE = 0x6000
FULL_SIZE = 0x40000
EXPECTED_FULL_OFFSET = 0x14000


@dataclass(frozen=True)
class XdfInfo:
    path: str
    base_offset: int
    region_size: int
    defined_items: int
    addressed_elements: int
    minimum_address: int
    maximum_address: int


def inspect_xdf(path: Path) -> tuple[XdfInfo, ET.Element]:
    root = ET.parse(path).getroot()
    header = root.find("XDFHEADER")
    if header is None:
        raise ValueError(f"{path}: missing XDFHEADER")
    base = header.find("BASEOFFSET")
    region = header.find("REGION")
    if base is None or region is None:
        raise ValueError(f"{path}: missing BASEOFFSET or REGION")

    addresses = [
        int(element.attrib["mmedaddress"], 0)
        for element in root.iter("EMBEDDEDDATA")
        if "mmedaddress" in element.attrib
    ]
    item_tags = {"XDFCONSTANT", "XDFTABLE", "XDFFLAG"}
    items = sum(1 for element in root if element.tag in item_tags)
    info = XdfInfo(
        path=str(path),
        base_offset=int(base.attrib["offset"], 0),
        region_size=int(region.attrib["size"], 0),
        defined_items=items,
        addressed_elements=len(addresses),
        minimum_address=min(addresses),
        maximum_address=max(addresses),
    )
    return info, root


def normalized_definition(root: ET.Element) -> bytes:
    clone = copy.deepcopy(root)
    header = clone.find("XDFHEADER")
    assert header is not None
    base = header.find("BASEOFFSET")
    region = header.find("REGION")
    assert base is not None and region is not None
    base.set("offset", "0")
    region.set("size", "0")
    return ET.tostring(clone)


def validate(partial_xdf: Path, full_xdf: Path, bins: list[Path]) -> dict:
    partial, partial_root = inspect_xdf(partial_xdf)
    full, full_root = inspect_xdf(full_xdf)
    errors: list[str] = []
    warnings: list[str] = []

    if partial.base_offset != 0:
        errors.append(f"24K XDF base offset is 0x{partial.base_offset:X}, expected 0")
    if full.base_offset != EXPECTED_FULL_OFFSET:
        errors.append(
            f"256K XDF base offset is 0x{full.base_offset:X}, "
            f"expected 0x{EXPECTED_FULL_OFFSET:X}"
        )
    if partial.region_size != PARTIAL_SIZE:
        warnings.append(
            f"24K XDF REGION metadata is 0x{partial.region_size:X}, not 0x6000; "
            "actual addresses remain inside the 24K BIN"
        )
    if full.region_size != FULL_SIZE:
        errors.append(f"256K XDF region is 0x{full.region_size:X}, expected 0x40000")
    if normalized_definition(partial_root) != normalized_definition(full_root):
        errors.append("XDF definitions differ beyond BASEOFFSET")
    if partial.maximum_address >= PARTIAL_SIZE:
        errors.append("an XDF address is outside the 24K calibration region")

    bin_results = []
    for path in bins:
        data = path.read_bytes()
        if len(data) == PARTIAL_SIZE:
            offset = partial.base_offset
            xdf = "24K"
        elif len(data) == FULL_SIZE:
            offset = full.base_offset
            xdf = "256K"
        else:
            errors.append(f"{path}: unsupported BIN size {len(data)}")
            continue
        effective_max = offset + partial.maximum_address
        in_bounds = effective_max < len(data)
        header_matches = data[offset:offset + 4] == b"\x4e\x00\xff\xff"
        if not in_bounds:
            errors.append(f"{path}: effective XDF address 0x{effective_max:X} is out of bounds")
        if not header_matches:
            errors.append(f"{path}: no calibration header at XDF base 0x{offset:X}")
        bin_results.append(
            {
                "path": str(path),
                "size": len(data),
                "xdf": xdf,
                "base_offset": offset,
                "effective_min": offset + partial.minimum_address,
                "effective_max": effective_max,
                "in_bounds": in_bounds,
                "header_matches": header_matches,
            }
        )

    return {
        "valid": not errors,
        "expected_relationship": "full_file_address = partial_address + 0x14000",
        "partial_xdf": asdict(partial),
        "full_xdf": asdict(full),
        "bins": bin_results,
        "warnings": warnings,
        "errors": errors,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("partial_xdf", type=Path)
    parser.add_argument("full_xdf", type=Path)
    parser.add_argument("bins", nargs="*", type=Path)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    report = validate(args.partial_xdf, args.full_xdf, args.bins)
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        state = "VALID" if report["valid"] else "INVALID"
        print(f"{state}: {report['expected_relationship']}")
        print(
            f"24K XDF: base=0x{report['partial_xdf']['base_offset']:X}, "
            f"items={report['partial_xdf']['defined_items']}, "
            f"addressed={report['partial_xdf']['addressed_elements']}"
        )
        print(
            f"256K XDF: base=0x{report['full_xdf']['base_offset']:X}, "
            f"items={report['full_xdf']['defined_items']}, "
            f"addressed={report['full_xdf']['addressed_elements']}"
        )
        for item in report["bins"]:
            print(
                f"  {item['path']}: {item['xdf']} base=0x{item['base_offset']:X} "
                f"effective=0x{item['effective_min']:X}-0x{item['effective_max']:X} "
                f"header={item['header_matches']} bounds={item['in_bounds']}"
            )
        for warning in report["warnings"]:
            print(f"WARNING: {warning}")
        for error in report["errors"]:
            print(f"ERROR: {error}")
    return 0 if report["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
