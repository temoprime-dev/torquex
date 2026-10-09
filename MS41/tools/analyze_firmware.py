#!/usr/bin/env python3
"""Identify and validate Siemens MS41 calibration/full-flash files.

The detector intentionally ignores filenames. Identification is based on file
layout, the MS41 calibration header, software-family strings, and calibration
CRC blocks.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path

PARTIAL_SIZE = 0x6000
FULL_SIZE = 0x40000
CALIBRATION_OFFSET = 0x14000
SOFTWARE_FAMILY = b"064101"


@dataclass(frozen=True)
class Analysis:
    path: str
    size: int
    sha256: str
    manufacturer: str | None
    ecu_family: str | None
    image_type: str | None
    calibration_offset: int | None
    software_family: str | None
    calibration_variant: str | None
    crc_blocks: int
    crc_valid: bool
    supported: bool
    reasons: list[str]


def _crc16(data: bytes, initial: int) -> int:
    """CRC-16/ARC used by the chained MS41 calibration blocks."""
    crc = initial
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc & 0xFFFF


def verify_calibration_crc(calibration: bytes) -> tuple[bool, int, list[str]]:
    if len(calibration) != PARTIAL_SIZE:
        return False, 0, [f"calibration size is {len(calibration)}, expected {PARTIAL_SIZE}"]

    initial = int.from_bytes(calibration[0x0E:0x10], "big")
    cursor = 0
    blocks = 0
    errors: list[str] = []

    while cursor + 2 <= len(calibration):
        checksum_address = int.from_bytes(calibration[cursor:cursor + 2], "little")
        if checksum_address == 0xFFFF:
            break
        if checksum_address <= cursor or checksum_address + 2 > len(calibration):
            errors.append(f"invalid CRC pointer 0x{checksum_address:04X} at 0x{cursor:04X}")
            break

        recorded = int.from_bytes(
            calibration[checksum_address:checksum_address + 2], "little"
        )
        calculated = _crc16(calibration[cursor:checksum_address], initial)
        blocks += 1
        if recorded != calculated:
            errors.append(
                f"CRC block {blocks} mismatch at 0x{checksum_address:04X}: "
                f"stored 0x{recorded:04X}, calculated 0x{calculated:04X}"
            )
        cursor = checksum_address + 2
    else:
        errors.append("CRC chain has no terminator")

    if blocks == 0:
        errors.append("no CRC blocks found")
    return not errors, blocks, errors


def _ascii(data: bytes) -> str | None:
    if not data or any(value < 0x20 or value > 0x7E for value in data):
        return None
    return data.decode("ascii")


def analyze(path: Path) -> Analysis:
    data = path.read_bytes()
    reasons: list[str] = []
    image_type: str | None = None
    calibration_offset: int | None = None

    if len(data) == PARTIAL_SIZE:
        image_type = "calibration-24K"
        calibration_offset = 0
    elif len(data) == FULL_SIZE:
        image_type = "full-flash-256K"
        calibration_offset = CALIBRATION_OFFSET
    else:
        reasons.append(
            f"unsupported size {len(data)}; expected {PARTIAL_SIZE} or {FULL_SIZE}"
        )

    calibration = (
        data[calibration_offset:calibration_offset + PARTIAL_SIZE]
        if calibration_offset is not None
        else b""
    )

    identity_area = calibration[0x0C:0x4A]
    header_valid = (
        len(calibration) == PARTIAL_SIZE
        and calibration[0:2] == b"\x4e\x00"
        and calibration[2:4] == b"\xff\xff"
        and identity_area.startswith(b"064101111")
        and identity_area.count(b"0060041010000") >= 3
    )
    if calibration_offset is not None and not header_valid:
        reasons.append("MS41 calibration header/software-family signature not found")

    crc_valid, crc_blocks, crc_errors = verify_calibration_crc(calibration)
    reasons.extend(crc_errors)

    software_family = SOFTWARE_FAMILY.decode("ascii") if header_valid else None
    # Eight ASCII bytes following the 0x20 0x5e marker identify the calibration
    # variant in all verified 1429861 fixtures.
    marker = calibration.find(b"\x20\x5e", 0x40, 0x60)
    calibration_variant = (
        _ascii(calibration[marker + 2:marker + 10]) if marker >= 0 else None
    )

    supported = image_type is not None and header_valid and crc_valid
    if supported:
        reasons.append(
            "Siemens MS41 signature confirmed from binary content; filename vendor text ignored"
        )

    return Analysis(
        path=str(path),
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        manufacturer="Siemens" if header_valid else None,
        ecu_family="MS41" if header_valid else None,
        image_type=image_type,
        calibration_offset=calibration_offset,
        software_family=software_family,
        calibration_variant=calibration_variant,
        crc_blocks=crc_blocks,
        crc_valid=crc_valid,
        supported=supported,
        reasons=reasons,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("paths", nargs="+", type=Path)
    parser.add_argument("--json", action="store_true", help="emit JSON")
    args = parser.parse_args()

    results = [analyze(path) for path in args.paths]
    if args.json:
        print(json.dumps([asdict(result) for result in results], indent=2))
    else:
        for result in results:
            state = "SUPPORTED" if result.supported else "REJECTED"
            print(
                f"{state} {result.path}: manufacturer={result.manufacturer or '-'} "
                f"ecu={result.ecu_family or '-'} type={result.image_type or '-'} "
                f"software={result.software_family or '-'} "
                f"variant={result.calibration_variant or '-'} "
                f"crc={result.crc_valid} ({result.crc_blocks} blocks)"
            )
            for reason in result.reasons:
                print(f"  - {reason}")
    return 0 if all(result.supported for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
