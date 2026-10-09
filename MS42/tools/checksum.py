#!/usr/bin/env python3
"""Verify and repair the three MS42 checksums, or refuse to emit the image.

Algorithm (reverse-engineered from the 16 stock images in ``MS42/firmware``
and cross-checked against the community profile ``bmw-ms42-0110c6-crc16-v1``):

* CRC-16/ARC: polynomial 0x8005, reflected form 0xA001 (shift right), init is
  the per-record seed, no input/output reflection flags are needed beyond the
  reflected polynomial, xor-out 0, table-driven byte at a time.  The two
  equivalent implementations (bitwise and table-driven) are checked against
  each other by the tests.
* Every record lives at a fixed address and describes itself:

      u16 stored CRC (little endian)
      u16 segment count (little endian)
      count * (u32 first address, u32 last address)   -- both inclusive

  The CRC is computed over the concatenation of the segments in the declared
  order, seeded with the record's fixed seed.  The segment table is read from
  the image, not assumed, so a damaged table is a verification failure, not a
  silent reinterpretation.

* Where the values live in a 512 KiB image:

      boot @ 0x3C24, seed 0x2D2D, 0x00000..0x03C23                  -> 0xDF13
      cal  @ 0x4FEE0, seed 0x3643, 0x48000..0x4F843                 -> per image
      prog @ 0x50306, seed 0x3030, 7 segments listed in CRC_SEGMENTS -> 0xF347

  The calibration file of a 64 KiB partial read is the same cal record at file
  offset 0x0FEE0; a bare 32 KiB calibration read keeps it at 0x07EE0 and still
  covers 0x48000..0x4F843.  The boot/prog records do not exist in those reads.

The repair direction is deliberately one-way and paranoid: bytes are only ever
written at the three stored-CRC positions, and the result is re-verified with
an independent pass before it is written out.  If re-verification fails, or if
any byte outside a CRC position moved, the tool raises instead of returning an
image - it must never produce a flashable file that is not known to verify.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path

try:
    from MS42.tools.analyze_firmware import (
        ADDRESS_BASES,
        CRC_RECORDS,
        CRC_RECORDS_PRESENT,
        FirmwareError,
        compute_record,
        identify_layout,
        parse_record,
        verify_record,
    )
except ModuleNotFoundError:  # Direct execution from MS42/tools.
    from analyze_firmware import (  # type: ignore[no-redef]
        ADDRESS_BASES,
        CRC_RECORDS,
        CRC_RECORDS_PRESENT,
        FirmwareError,
        compute_record,
        identify_layout,
        parse_record,
        verify_record,
    )

#: The expected segment geometry, kept here as documentation and as a tripwire:
#: the tests assert that every stock image declares exactly these segments.
CRC_SEGMENTS: dict[str, tuple[tuple[int, int], ...]] = {
    "boot": ((0x00000, 0x03C23),),
    "cal": ((0x48000, 0x4F843),),
    "prog": (
        (0x500A4, 0x50305),
        (0x50342, 0x52EFD),
        (0x60000, 0x60C43),
        (0x70000, 0x7EC65),
        (0x11000, 0x1FFFD),
        (0x20000, 0x2FFFD),
        (0x30000, 0x3FFFF),
    ),
}


@dataclass
class RecordOutcome:
    name: str
    address: int
    file_offset: int | None
    stored_before: int
    computed: int
    stored_after: int
    changed: bool
    valid_before: bool
    valid_after: bool
    segments: list[tuple[int, int]]
    coverage_bytes: int


@dataclass
class ChecksumResult:
    path: str
    image_type: str
    address_base: int
    size: int
    sha256_before: str
    sha256_after: str
    records: list[RecordOutcome] = field(default_factory=list)
    repaired: bool = False
    valid_before: bool = False
    valid_after: bool = False
    bytes_written: int = 0
    findings: list[dict[str, str]] = field(default_factory=list)


def present_records(image_type: str) -> tuple[str, ...]:
    return CRC_RECORDS_PRESENT.get(image_type, ())


def _record_report(image: bytes | bytearray, name: str, address_base: int) -> RecordOutcome:
    record = verify_record(image, name, address_base)
    return RecordOutcome(
        name=name,
        address=record.offset,
        file_offset=record.offset - address_base,
        stored_before=record.stored,
        computed=record.computed,
        stored_after=record.stored,
        changed=False,
        valid_before=record.valid,
        valid_after=record.valid,
        segments=list(record.segments),
        coverage_bytes=record.coverage_bytes,
    )


def inspect(path: Path) -> ChecksumResult:
    """Read an image and report every present record without modifying it."""
    data = path.read_bytes()
    image_type, _calibration_offset = identify_layout(len(data))
    if image_type is None:
        raise FirmwareError(
            f"unsupported size {len(data)}; expected 0x80000 (512 KiB), 0x10000 (64 KiB) "
            f"or 0x8000 (32 KiB)"
        )
    address_base = ADDRESS_BASES[image_type]
    names = present_records(image_type)
    if not names:
        raise FirmwareError(f"no CRC records are defined for layout {image_type!r}")
    result = ChecksumResult(
        path=str(path),
        image_type=image_type,
        address_base=address_base,
        size=len(data),
        sha256_before=hashlib.sha256(data).hexdigest(),
        sha256_after=hashlib.sha256(data).hexdigest(),
    )
    try:
        result.records = [_record_report(data, name, address_base) for name in names]
    except FirmwareError as exc:
        result.findings.append({
            "code": "CRC_RECORD_UNREADABLE", "severity": "error", "message": str(exc),
        })
        result.valid_before = False
        result.valid_after = False
        return result
    result.valid_before = all(record.valid_before for record in result.records)
    result.valid_after = result.valid_before
    for record in result.records:
        if not record.valid_before:
            result.findings.append({
                "code": "CRC_MISMATCH",
                "severity": "error",
                "message": f"{record.name}: stored 0x{record.stored_before:04X}, "
                           f"computed 0x{record.computed:04X}",
            })
    return result


def repair(data: bytes, image_type: str) -> tuple[bytearray, ChecksumResult]:
    """Return a repaired copy of ``data`` plus the per-record outcome.

    Raises :class:`FirmwareError` when a record cannot be read, when the image
    type has no records, or when the re-verification pass fails.  The caller
    gets either a fully verified image or an exception - never a half-fixed one.
    """
    address_base = ADDRESS_BASES[image_type]
    names = present_records(image_type)
    if not names:
        raise FirmwareError(f"no CRC records are defined for layout {image_type!r}")

    repaired = bytearray(data)
    outcomes: list[RecordOutcome] = []
    written = 0
    try:
        for name in names:
            address, seed = CRC_RECORDS[name]
            outcome = _record_report(data, name, address_base)
            outcome.stored_after = outcome.computed
            outcome.changed = outcome.stored_before != outcome.computed
            file_offset = address - address_base
            if outcome.changed:
                repaired[file_offset:file_offset + 2] = outcome.computed.to_bytes(2, "little")
                written += 2
            outcome.valid_after = True
            outcomes.append(outcome)
    except FirmwareError:
        raise

    # Independent re-verification of the buffer we are about to hand out.
    for name, outcome in zip(names, outcomes):
        record = verify_record(repaired, name, address_base)
        if not record.valid:
            raise FirmwareError(
                f"refusing to emit the image: {name} still does not verify after repair "
                f"(stored 0x{record.stored:04X}, computed 0x{record.computed:04X})"
            )
        outcome.valid_after = True

    positions = set()
    for name in names:
        file_offset = CRC_RECORDS[name][0] - address_base
        positions.update((file_offset, file_offset + 1))
    for index, (before, after) in enumerate(zip(data, repaired)):
        if before != after and index not in positions:
            raise FirmwareError(
                f"refusing to emit the image: byte 0x{index:05X} changed outside a CRC position"
            )

    result = ChecksumResult(
        path="",
        image_type=image_type,
        address_base=address_base,
        size=len(data),
        sha256_before=hashlib.sha256(data).hexdigest(),
        sha256_after=hashlib.sha256(bytes(repaired)).hexdigest(),
        records=outcomes,
        repaired=written > 0,
        valid_before=all(record.valid_before for record in outcomes),
        valid_after=all(record.valid_after for record in outcomes),
        bytes_written=written,
    )
    for record in outcomes:
        if record.changed:
            result.findings.append({
                "code": "CRC_REPAIRED",
                "severity": "info",
                "message": f"{record.name}: 0x{record.stored_before:04X} -> "
                           f"0x{record.stored_after:04X}",
            })
    return repaired, result


def run(args: argparse.Namespace) -> int:
    path = Path(args.bin)
    data = path.read_bytes()
    image_type, _calibration_offset = identify_layout(len(data))
    if image_type is None:
        print(f"unsupported size {len(data)}; expected 0x80000, 0x10000 or 0x8000")
        return 1

    if args.out is None:
        result = inspect(path)
        if args.report:
            args.report.write_text(json.dumps(_payload(result), indent=2), encoding="utf-8")
        _print_report(result)
        return 0 if result.valid_before else 3

    try:
        repaired, result = repair(data, image_type)
    except FirmwareError as exc:
        print(f"refused: {exc}")
        return 4
    result.path = str(path)
    if args.out is not None:
        Path(args.out).write_bytes(bytes(repaired))
    if args.report:
        args.report.write_text(json.dumps(_payload(result), indent=2), encoding="utf-8")
    _print_report(result)
    return 0


def _payload(result: ChecksumResult) -> dict:
    return {
        "path": result.path,
        "image_type": result.image_type,
        "address_base": result.address_base,
        "size": result.size,
        "sha256_before": result.sha256_before,
        "sha256_after": result.sha256_after,
        "repaired": result.repaired,
        "valid_before": result.valid_before,
        "valid_after": result.valid_after,
        "bytes_written": result.bytes_written,
        "records": [
            {
                "name": record.name,
                "address": record.address,
                "file_offset": record.file_offset,
                "stored_before": record.stored_before,
                "computed": record.computed,
                "stored_after": record.stored_after,
                "changed": record.changed,
                "valid_before": record.valid_before,
                "valid_after": record.valid_after,
                "segments": [list(segment) for segment in record.segments],
                "coverage_bytes": record.coverage_bytes,
            }
            for record in result.records
        ],
        "findings": result.findings,
    }


def _print_report(result: ChecksumResult) -> None:
    verb = "REPAIRED" if result.repaired else "OK" if result.valid_before else "INVALID"
    print(f"{verb} {result.path}: {result.image_type}, "
          f"{len(result.records)} record(s) checked, {result.bytes_written} byte(s) written")
    for record in result.records:
        state = "OK" if record.valid_after else "BAD"
        print(f"  - {record.name}: stored 0x{record.stored_before:04X} -> "
              f"0x{record.stored_after:04X} (computed 0x{record.computed:04X}) "
              f"{state}, {record.coverage_bytes} bytes covered")
    for finding in result.findings:
        if finding["severity"] != "info":
            print(f"  [{finding['severity']}] {finding['code']}: {finding['message']}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bin", type=Path, help="image to verify (or repair with --out)")
    parser.add_argument("--out", type=Path,
                        help="write the repaired image here; omitted means verify only")
    parser.add_argument("--report", type=Path, help="write the JSON report here")
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
