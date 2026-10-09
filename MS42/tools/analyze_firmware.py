#!/usr/bin/env python3
"""Identify and validate Siemens MS42 firmware and calibration reads.

The detector ignores filenames. Identification is based on file size, the
calibration dataset header, the operating-system ID string, and the three
self-describing CRC16 records the MS42 boot code verifies before it will run the
engine.

MS42 memory layout (full 512 KiB image):

============================  ==========  ==========================
Range                         Size        Section
============================  ==========  ==========================
0x00000..0x0FFFF              64 KiB      Bootloader and UIF
0x10000..0x3FFFF              192 KiB     Program code 1
0x40000..0x47FFF              32 KiB      Unused
0x48000..0x4FFFF              32 KiB      Calibration data
0x50000..0x7FFFF              192 KiB     Program code 2
============================  ==========  ==========================

Three CRC16 records are stored as self-describing descriptors. Each record is

    u16 stored_crc (little endian)
    u16 segment_count (little endian)
    segment_count * (u32 start, u32 end)  little endian, ends inclusive

The stored CRC is CRC-16/ARC (reflected polynomial 0xA001) over the
concatenation of the segments, seeded with a fixed value, then written little
endian. Only the boot CRC and the program CRC live outside the calibration
area, and their own descriptor bytes are excluded from the covered segments.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path

FULL_SIZE = 0x80000               # 512 KiB complete flash image
PARTIAL_SIZE = 0x10000            # 64 KiB community "partial read" (0x40000..0x4FFFF)
CALIBRATION_SIZE = 0x8000         # 32 KiB calibration area
READ_32K_SIZE = CALIBRATION_SIZE  # 32 KiB bare calibration read

PARTIAL_BASE = 0x40000            # first byte of a 64 KiB partial read
CALIBRATION_BASE = 0x48000        # first byte of the calibration area
CALIBRATION_OFFSET = CALIBRATION_BASE  # kept for callers used to MS41 naming

OSID_OFFSET = 0x08                # inside the calibration area
OSID = b"0110C6"
DATASET_ID_OFFSET = 0x00          # inside the calibration area
DATASET_ID_LENGTH = 8

#: record name -> (descriptor offset in the full image, CRC seed)
CRC_RECORDS: dict[str, tuple[int, int]] = {
    "boot": (0x03C24, 0x2D2D),
    "cal": (0x4FEE0, 0x3643),
    "prog": (0x50306, 0x3030),
}

#: value subtracted from a full-image address to obtain a file offset
ADDRESS_BASES = {
    "full-flash-512K": 0,
    "partial-64K": PARTIAL_BASE,
    "calibration-32K": CALIBRATION_BASE,
}

CRC_RECORDS_PRESENT = {
    "full-flash-512K": ("boot", "cal", "prog"),
    "partial-64K": ("cal",),
    "calibration-32K": ("cal",),
}


class FirmwareError(ValueError):
    """Raised when an image cannot be read as MS42."""


@dataclass
class CrcRecord:
    name: str
    offset: int
    stored: int
    computed: int
    seed: int
    segments: list[tuple[int, int]]
    valid: bool
    coverage_bytes: int


@dataclass
class Analysis:
    path: str
    size: int
    sha256: str
    manufacturer: str | None
    ecu_family: str | None
    image_type: str | None
    calibration_offset: int | None
    calibration_size: int | None
    #: Value to subtract from a full-image address to get a file offset.
    #: 0 for a 512 KiB full read, 0x40000 for a 64 KiB partial read,
    #: 0x48000 for a bare 32 KiB calibration read.
    address_base: int | None
    osid: str | None
    dataset_id: str | None
    crc_records: list[CrcRecord] = field(default_factory=list)
    crc_valid: bool = False
    supported: bool = False
    reasons: list[str] = field(default_factory=list)


def _crc16(data: bytes, seed: int) -> int:
    """CRC-16/ARC (reflected 0xA001) with an explicit seed, as used by MS42."""
    crc = seed & 0xFFFF
    for value in data:
        crc ^= value
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc & 0xFFFF


CRC_TABLE: tuple[int, ...] = tuple(_crc16(bytes([value]), 0x0000) for value in range(256))


def crc16(data: bytes, seed: int) -> int:
    """Table-driven CRC-16/ARC; byte-for-byte equal to :func:`_crc16`."""
    crc = seed & 0xFFFF
    for value in data:
        crc = (crc >> 8) ^ CRC_TABLE[(crc ^ value) & 0xFF]
    return crc & 0xFFFF


def parse_record(
    image: bytes | bytearray, offset: int, address_base: int = 0
) -> tuple[int, list[tuple[int, int]]]:
    """Read one self-describing CRC record: (stored crc, absolute segments)."""
    if offset + 4 > len(image):
        raise FirmwareError(f"CRC record at 0x{offset:05X} is outside the image")
    if offset < 0:
        raise FirmwareError(f"CRC record at full-image address 0x{offset + 0:05X} is absent")
    stored = int.from_bytes(image[offset:offset + 2], "little")
    count = int.from_bytes(image[offset + 2:offset + 4], "little")
    if count == 0 or count > 64:
        raise FirmwareError(
            f"CRC record at 0x{offset:05X} declares an implausible segment count {count}"
        )
    if offset + 4 + 8 * count > len(image):
        raise FirmwareError(f"CRC record at 0x{offset:05X} is truncated")
    segments: list[tuple[int, int]] = []
    cursor = offset + 4
    for index in range(count):
        start = int.from_bytes(image[cursor:cursor + 4], "little")
        end = int.from_bytes(image[cursor + 4:cursor + 8], "little")
        cursor += 8
        if start > end or end - address_base >= len(image) or start - address_base < 0:
            raise FirmwareError(
                f"CRC record at 0x{offset:05X} has invalid segment {index}: "
                f"0x{start:05X}..0x{end:05X}"
            )
        segments.append((start, end))
    return stored, segments


def compute_record(
    image: bytes | bytearray,
    seed: int,
    segments: list[tuple[int, int]],
    address_base: int = 0,
) -> int:
    """CRC over absolute-address segments read from an image at ``address_base``."""
    computed = seed & 0xFFFF
    for start, end in segments:
        computed = crc16(bytes(image[start - address_base:end - address_base + 1]), computed)
    return computed


def file_offset(absolute: int, address_base: int) -> int:
    return absolute - address_base


def verify_record(image: bytes | bytearray, name: str, address_base: int = 0) -> CrcRecord:
    offset, seed = CRC_RECORDS[name]
    stored, segments = parse_record(image, offset - address_base, address_base)
    computed = compute_record(image, seed, segments, address_base)
    return CrcRecord(
        name=name,
        offset=offset,
        stored=stored,
        computed=computed,
        seed=seed,
        segments=segments,
        valid=stored == computed,
        coverage_bytes=sum(end - start + 1 for start, end in segments),
    )


def _ascii(data: bytes) -> str | None:
    if not data or any(value < 0x20 or value > 0x7E for value in data):
        return None
    return data.decode("ascii")


def identify_layout(size: int) -> tuple[str | None, int | None]:
    """Return (image type, calibration offset) for a supported file size."""
    if size == FULL_SIZE:
        return "full-flash-512K", CALIBRATION_BASE
    if size == PARTIAL_SIZE:
        return "partial-64K", CALIBRATION_BASE - PARTIAL_BASE
    if size == READ_32K_SIZE:
        return "calibration-32K", 0
    return None, None


def analyze(path: Path) -> Analysis:
    data = path.read_bytes()
    reasons: list[str] = []
    image_type, calibration_offset = identify_layout(len(data))

    address_base = ADDRESS_BASES.get(image_type) if image_type else None

    if image_type is None:
        reasons.append(
            f"unsupported size {len(data)}; expected {FULL_SIZE} (512 KiB full), "
            f"{PARTIAL_SIZE} (64 KiB partial) or {READ_32K_SIZE} (32 KiB calibration read)"
        )
        return Analysis(
            path=str(path),
            size=len(data),
            sha256=hashlib.sha256(data).hexdigest(),
            manufacturer=None,
            ecu_family=None,
            image_type=None,
            calibration_offset=None,
            calibration_size=None,
            address_base=None,
            osid=None,
            dataset_id=None,
            reasons=reasons,
        )

    assert calibration_offset is not None
    calibration = data[calibration_offset:calibration_offset + CALIBRATION_SIZE]
    osid = _ascii(calibration[OSID_OFFSET:OSID_OFFSET + len(OSID)])
    dataset_id = _ascii(calibration[DATASET_ID_OFFSET:DATASET_ID_OFFSET + DATASET_ID_LENGTH])

    header_valid = osid == OSID.decode("ascii")
    if not header_valid:
        reasons.append(
            f"MS42 operating-system ID {osid!r} at 0x{CALIBRATION_BASE + OSID_OFFSET:05X} "
            f"is not {OSID.decode('ascii')!r}"
        )

    records: list[CrcRecord] = []
    records_expected = CRC_RECORDS_PRESENT[image_type]
    records_unreadable = False
    for name in records_expected:
        try:
            record = verify_record(data, name, address_base or 0)
        except FirmwareError as exc:
            reasons.append(f"{name} CRC record is unreadable: {exc}")
            records_unreadable = True
            continue
        records.append(record)
        if not record.valid:
            reasons.append(
                f"{name} CRC mismatch at 0x{record.offset:05X}: stored "
                f"0x{record.stored:04X}, computed 0x{record.computed:04X}"
            )

    crc_valid = (
        not records_unreadable
        and len(records) == len(records_expected)
        and all(record.valid for record in records)
    )
    supported = image_type is not None and header_valid and crc_valid
    if supported:
        reasons.append(
            "Siemens MS42 0110C6 signature confirmed from binary content; "
            "filename vendor text and marketing variant are ignored"
        )
    if image_type != "full-flash-512K":
        reasons.append(
            "This is not a complete 512 KiB image; only the calibration CRC record "
            "is present and only calibration data can be patched"
        )

    return Analysis(
        path=str(path),
        size=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
        manufacturer="Siemens" if header_valid else None,
        ecu_family="MS42" if header_valid else None,
        image_type=image_type,
        calibration_offset=calibration_offset,
        calibration_size=CALIBRATION_SIZE,
        address_base=address_base,
        osid=osid if header_valid else None,
        dataset_id=dataset_id,
        crc_records=records,
        crc_valid=crc_valid,
        supported=supported,
        reasons=reasons,
    )


def load_supported(path: Path) -> tuple[bytearray, Analysis]:
    """Read an image and refuse anything that is not a verified MS42 file."""
    analysis = analyze(path)
    if not analysis.supported:
        raise FirmwareError(
            f"{path} is not a supported MS42 image: {'; '.join(analysis.reasons)}"
        )
    return bytearray(path.read_bytes()), analysis


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
                f"osid={result.osid or '-'} dataset={result.dataset_id or '-'}"
            )
            for record in result.crc_records:
                print(
                    f"  - {record.name}: stored 0x{record.stored:04X}, "
                    f"computed 0x{record.computed:04X}, "
                    f"{len(record.segments)} segment(s), "
                    f"{record.coverage_bytes} bytes covered, "
                    f"{'OK' if record.valid else 'INVALID'}"
                )
            for reason in result.reasons:
                print(f"  - {reason}")
    return 0 if all(result.supported for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
