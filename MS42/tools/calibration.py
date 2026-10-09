#!/usr/bin/env python3
"""Patch MS42 calibrations inside a strict allowlist, with a full diff report.

This is the first MS42 component that can damage a flash image, so it is built
the same way as the MS41 patch core, only the checksum layer differs:

* default deny - a symbol is patchable only when the policy file lists it;
* only calibration data inside a CRC-covered segment is writable; program code,
  the bootloader and the dataset header are never touched;
* the source image must pass every CRC record it contains before anything is
  planned;
* every record whose covered bytes changed is recomputed after patching, and the
  whole image must verify again;
* the set of changed bytes must equal the planned data bytes plus the recomputed
  CRC words exactly, and nothing else;
* a plan is refused when a value cannot be represented precisely enough.

MS42 stores three self-describing CRC16 records (boot, calibration, program).
A calibration-only patch therefore rewrites one 16-bit word, at 0x4FEE0, and
leaves the boot and program records untouched - the patch core proves that by
re-reading them rather than by assuming it.

Nothing here invents a value. Every number comes from the caller.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

try:
    from MS42.tools.analyze_firmware import (
        CALIBRATION_SIZE,
        CRC_RECORDS,
        CRC_RECORDS_PRESENT,
        FirmwareError,
        analyze,
        compute_record,
        crc16,
        file_offset,
        parse_record,
    )
    from MS42.tools import xdf as xdf_module
    from MS42.tools.xdf import XdfError, XdfObject, load_definition
except ModuleNotFoundError:  # Direct execution from MS42/tools.
    from analyze_firmware import (  # type: ignore[no-redef]
        CALIBRATION_SIZE,
        CRC_RECORDS,
        CRC_RECORDS_PRESENT,
        FirmwareError,
        analyze,
        compute_record,
        crc16,
        file_offset,
        parse_record,
    )
    import xdf as xdf_module  # type: ignore[no-redef]
    from xdf import XdfError, XdfObject, load_definition  # type: ignore[no-redef]

ROOT = Path(__file__).parents[1]
DEFAULT_XDF = ROOT / "definitions" / "Siemens_MS42_0110C6_ENG_512K_v1.3.xdf"
DEFAULT_POLICY = ROOT / "policies" / "stage_policy.json"
DEFAULT_QUANTIZATION_PERCENT = 1.0


class PatchError(ValueError):
    """Raised when a patch cannot be applied safely."""


@dataclass(frozen=True)
class PolicyEntry:
    symbol: str
    minimum: float | None = None
    maximum: float | None = None
    max_delta_percent: float | None = None
    max_cells_per_patch: int | None = None
    require_monotonic: bool = False
    #: Relative tolerance for the cell quantum, overriding the global one. Small
    #: tables (sub-millisecond wall wetting and dead time) cannot honour a 1 %
    #: rule: their step is 0.032 ms, so a 0.2 ms cell is only representable
    #: within 4 %. The override is explicit, per object, and quoted in the note.
    max_quantization_error_percent: float | None = None
    #: Which definition to use when a title appears more than once in the XDF.
    #: MS42 declares several views of the same bytes (the MAF curve exists as a
    #: 256x1 table and as a 16x16 table, and both exist again for the community
    #: "MAF hack" with a doubled scale). Guessing between them would be unsafe,
    #: so a duplicate title is unpatchable until the policy picks a definition.
    xdf_index: int | None = None
    #: Read-only objects are still visible to analysis, but a write is allowed
    #: only when the request carries the exact policy-declared context.
    read_only: bool = False
    write_context: str | None = None
    note: str = ""


@dataclass
class Policy:
    version: int
    entries: dict[str, PolicyEntry]
    max_quantization_error_percent: float = DEFAULT_QUANTIZATION_PERCENT
    description: str = ""

    @classmethod
    def load(cls, path: Path) -> "Policy":
        data = json.loads(path.read_text(encoding="utf-8"))
        entries: dict[str, PolicyEntry] = {}
        for symbol, raw in (data.get("objects") or {}).items():
            entries[symbol] = PolicyEntry(
                symbol=symbol,
                minimum=raw.get("min"),
                maximum=raw.get("max"),
                max_delta_percent=raw.get("max_delta_percent"),
                max_cells_per_patch=raw.get("max_cells_per_patch"),
                require_monotonic=bool(raw.get("require_monotonic", False)),
                max_quantization_error_percent=raw.get("max_quantization_error_percent"),
                xdf_index=raw.get("xdf_index"),
                read_only=bool(raw.get("read_only", False)),
                write_context=raw.get("write_context"),
                note=raw.get("note", ""),
            )
        return cls(
            version=int(data.get("version", 1)),
            entries=entries,
            max_quantization_error_percent=float(
                data.get("max_quantization_error_percent", DEFAULT_QUANTIZATION_PERCENT)
            ),
            description=data.get("description", ""),
        )

    def entry(self, symbol: str) -> PolicyEntry | None:
        return self.entries.get(symbol)


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    message: str


@dataclass
class PlannedChange:
    symbol: str
    index: int
    address: int
    byte_offsets: list[int]
    old_raw: int
    new_raw: int
    old_engineering: float
    new_engineering: float
    requested_engineering: float
    quantization_error_percent: float
    delta_percent: float | None
    units: str
    kind: str
    crc_record: str = ""


@dataclass
class PatchPlan:
    status: str
    source: str
    source_sha256: str
    image_type: str | None
    calibration_offset: int | None
    address_base: int | None = None
    findings: list[Finding] = field(default_factory=list)
    changes: list[PlannedChange] = field(default_factory=list)
    policy_version: int = 1
    policy_entries: dict[str, PolicyEntry] = field(default_factory=dict)
    objects_available: int = 0
    unrepresentable_objects: list[str] = field(default_factory=list)
    crc_records_verified: int = 0


@dataclass
class PatchResult:
    status: str
    source: str
    output: str | None
    source_sha256: str
    output_sha256: str | None
    changes: list[PlannedChange]
    value_bytes_changed: int
    crc_bytes_changed: int
    total_bytes_changed: int
    crc_records_recomputed: int
    crc_valid_after: bool | None
    unplanned_byte_changes: list[int]
    findings: list[Finding]
    dry_run: bool


def _finding(findings: list[Finding], code: str, severity: str, message: str) -> None:
    findings.append(Finding(code, severity, message))


def load_calibration(path: Path) -> tuple[bytearray, int, str, int]:
    """Return (whole image, calibration offset, image type, address base)."""
    analysis = analyze(path)
    if not analysis.supported:
        raise PatchError(
            f"{path} is not a supported MS42 image: {'; '.join(analysis.reasons)}"
        )
    assert analysis.calibration_offset is not None
    assert analysis.address_base is not None
    return (
        bytearray(path.read_bytes()),
        analysis.calibration_offset,
        str(analysis.image_type),
        analysis.address_base,
    )


def record_segments(
    image: bytes | bytearray, name: str, address_base: int
) -> list[tuple[int, int]]:
    """Absolute segments of one CRC record, or an empty list when it is absent."""
    offset = CRC_RECORDS[name][0] - address_base
    if offset < 0 or offset + 4 > len(image):
        return []
    _, segments = parse_record(image, offset, address_base)
    return segments


def find_record_for_absolute(segments_by_record: dict[str, list[tuple[int, int]]],
                             absolute: int) -> str | None:
    for name, segments in segments_by_record.items():
        if any(start <= absolute <= end for start, end in segments):
            return name
    return None


def covered_end_relative(
    segments: list[tuple[int, int]], calibration_offset: int, address_base: int
) -> int:
    """Exclusive calibration-relative end of the widest covered record.

    Everything at or above this address is outside every CRC record, so a write
    there could never be detected by the ECU.
    """
    end = 0
    for start, end_absolute in segments:
        if start - address_base < calibration_offset + CALIBRATION_SIZE:
            end = max(end, end_absolute - address_base - calibration_offset + 1)
    return end


def crc_storage_offsets(image: bytes | bytearray, address_base: int) -> list[int]:
    """File offsets of the stored CRC words of every record present."""
    offsets: list[int] = []
    for name, (absolute, _seed) in CRC_RECORDS.items():
        offset = absolute - address_base
        if 0 <= offset and offset + 2 <= len(image):
            try:
                parse_record(image, offset, address_base)
            except FirmwareError:
                continue
            offsets.extend((offset, offset + 1))
    return offsets


def recompute_records(
    image: bytearray, address_base: int, changed_absolute: set[int]
) -> list[str]:
    """Recompute every record whose covered bytes changed; return their names."""
    recomputed: list[str] = []
    for name, (absolute_offset, seed) in CRC_RECORDS.items():
        offset = absolute_offset - address_base
        if offset < 0 or offset + 4 > len(image):
            continue
        try:
            stored, segments = parse_record(image, offset, address_base)
        except FirmwareError as exc:
            raise PatchError(f"{name} CRC record is unreadable: {exc}") from exc
        del stored
        touched = any(
            any(start <= changed <= end for changed in changed_absolute)
            for start, end in segments
        )
        if not touched:
            # A record whose covered bytes did not change must still be valid; if
            # it is not, the source is damaged and the patch must not proceed.
            computed = compute_record(image, seed, segments, address_base)
            if computed != int.from_bytes(image[offset:offset + 2], "little"):
                raise PatchError(f"{name} CRC was invalid before the patch")
            continue
        computed = compute_record(image, seed, segments, address_base)
        image[offset:offset + 2] = computed.to_bytes(2, "little")
        recomputed.append(name)
    return recomputed


def verify_records(image: bytes | bytearray, address_base: int) -> tuple[bool, list[str]]:
    """Verify every CRC record present in the image."""
    names = [name for name, (absolute, _) in CRC_RECORDS.items()
             if 0 <= absolute - address_base and absolute - address_base + 4 <= len(image)]
    errors: list[str] = []
    checked = 0
    for name in names:
        offset = CRC_RECORDS[name][0] - address_base
        try:
            stored, segments = parse_record(image, offset, address_base)
        except FirmwareError as exc:
            errors.append(f"{name}: {exc}")
            continue
        computed = compute_record(image, CRC_RECORDS[name][1], segments, address_base)
        checked += 1
        if computed != stored:
            errors.append(
                f"{name} CRC mismatch at 0x{CRC_RECORDS[name][0]:05X}: "
                f"stored 0x{stored:04X}, computed 0x{computed:04X}"
            )
    if not checked:
        errors.append("no CRC records found")
    return not errors, errors


def _resolve_objects(
    title: str, objects: dict[str, list[XdfObject]], entry: PolicyEntry | None = None
) -> XdfObject:
    matches = objects.get(title)
    if not matches:
        raise PatchError(f"{title!r} is not a known XDF object")
    if len(matches) > 1:
        if entry is not None and entry.xdf_index is not None:
            if not 0 <= entry.xdf_index < len(matches):
                raise PatchError(
                    f"{title!r} policy selects definition {entry.xdf_index}, but the XDF "
                    f"has {len(matches)}"
                )
            return matches[entry.xdf_index]
        shapes = ", ".join(
            f"[{i}] {o.rows}x{o.columns} x{o.scaler.a:g}"
            for i, o in enumerate(matches)
        )
        raise PatchError(
            f"{title!r} has {len(matches)} definitions ({shapes}); set xdf_index in "
            f"the policy to choose one explicitly"
        )
    return matches[0]


def _quantization_error_percent(requested: float, achieved: float) -> float:
    scale = max(abs(requested), 1e-9)
    return abs(achieved - requested) / scale * 100.0


def plan_patches(
    image: bytearray,
    calibration_offset: int,
    address_base: int,
    image_type: str,
    objects: dict[str, list[XdfObject]],
    problems: list[str],
    requests: list[dict[str, Any]],
    policy: Policy,
) -> PatchPlan:
    segments_by_record = {
        name: record_segments(image, name, address_base)
        for name in CRC_RECORDS_PRESENT.get(image_type, ())
    }
    segments_by_record = {name: segs for name, segs in segments_by_record.items() if segs}
    covered_end = covered_end_relative(
        [segment for segments in segments_by_record.values() for segment in segments],
        calibration_offset,
        address_base,
    )
    source_hash = hashlib.sha256(bytes(image)).hexdigest()
    plan = PatchPlan(
        status="PLANNED",
        source="",
        source_sha256=source_hash,
        image_type=image_type,
        calibration_offset=calibration_offset,
        address_base=address_base,
        policy_version=policy.version,
        policy_entries=dict(policy.entries),
        objects_available=sum(len(value) for value in objects.values()),
        unrepresentable_objects=problems,
    )
    ok, errors = verify_records(image, address_base)
    plan.crc_records_verified = len(segments_by_record)
    if not ok:
        _finding(plan.findings, "SOURCE_CRC_INVALID", "reject",
                 "Source CRC records are invalid: " + "; ".join(errors))
    if problems:
        _finding(plan.findings, "XDF_OBJECTS_UNREPRESENTABLE", "warning",
                 f"{len(problems)} XDF object(s) cannot be represented and are not patchable.")
    if not covered_end:
        _finding(plan.findings, "NO_CRC_COVERAGE", "reject",
                 "No CRC-covered calibration segment was found; nothing can be verified.")

    used_ranges: dict[int, str] = {}
    for request in requests:
        symbol = request.get("symbol")
        if not isinstance(symbol, str):
            _finding(plan.findings, "REQUEST_INVALID", "reject",
                     f"Patch request without a symbol: {request!r}")
            continue
        if "value" not in request:
            _finding(plan.findings, "REQUEST_INVALID", "reject",
                     f"Patch for {symbol} has no value")
            continue
        try:
            value = float(request["value"])
        except (TypeError, ValueError):
            _finding(plan.findings, "REQUEST_INVALID", "reject",
                     f"Patch value for {symbol} is not numeric: {request['value']!r}")
            continue

        entry = policy.entry(symbol)
        if entry is None:
            _finding(plan.findings, "SYMBOL_NOT_ALLOWED", "reject",
                     f"{symbol} is not in the patch allowlist; nothing is changed by default.")
            continue
        if entry.read_only:
            context_ok = (
                entry.write_context is not None
                and request.get("write_context") == entry.write_context
            )
            link_ok = (
                entry.write_context != "ignition_optm_link"
                or request.get("linked_to") == "ip_iga_ron_98_pl_ivvt__n__maf"
            )
            if not (context_ok and link_ok):
                _finding(plan.findings, "READ_ONLY", "reject",
                         f"{symbol} is marked read-only; only its explicit policy context may write it")
                continue
        elif entry.write_context is not None and request.get("write_context") != entry.write_context:
            _finding(plan.findings, "WRITE_CONTEXT_REQUIRED", "reject",
                     f"{symbol} is writable only in context {entry.write_context!r}")
            continue
        try:
            obj = _resolve_objects(symbol, objects, entry)
        except PatchError as exc:
            _finding(plan.findings, "SYMBOL_UNKNOWN", "reject", str(exc))
            continue

        index = request.get("index", 0)
        if not isinstance(index, int):
            _finding(plan.findings, "REQUEST_INVALID", "reject",
                     f"Patch index for {symbol} must be an integer")
            continue
        if obj.kind == "constant" and index != 0:
            _finding(plan.findings, "REQUEST_INVALID", "reject",
                     f"{symbol} is a single constant; index must be omitted or 0")
            continue
        if not 0 <= index < obj.element_count:
            _finding(plan.findings, "INDEX_OUT_OF_RANGE", "reject",
                     f"{symbol} has {obj.element_count} element(s); index {index} is invalid")
            continue

        raw_float = obj.to_raw(value)
        new_raw = int(round(raw_float))
        limit = 1 << obj.element_size_bits
        if not 0 <= new_raw < limit:
            _finding(plan.findings, "VALUE_NOT_REPRESENTABLE", "reject",
                     f"{symbol}={value} needs raw {raw_float:.2f}, outside 0..{limit - 1} "
                     f"for {obj.element_size_bits}-bit storage")
            continue
        achieved = obj.to_engineering(new_raw)

        offsets = list(obj.byte_offsets(index))
        clash = next((used_ranges[offset] for offset in offsets if offset in used_ranges), None)
        if clash:
            _finding(plan.findings, "OVERLAPPING_WRITES", "reject",
                     f"{symbol} overlaps a planned write for {clash}")
            continue

        quantisation = _quantization_error_percent(value, achieved)
        tolerance = (entry.max_quantization_error_percent
                     if entry.max_quantization_error_percent is not None
                     else policy.max_quantization_error_percent)
        if quantisation > tolerance:
            _finding(plan.findings, "QUANTIZATION_TOO_COARSE", "reject",
                     f"{symbol}={value} resolves to {achieved:.6g} "
                     f"({quantisation:.2f}% away), beyond the "
                     f"{tolerance:.2f}% limit for this object")
            continue
        if entry.minimum is not None and achieved < entry.minimum:
            _finding(plan.findings, "POLICY_MIN_VIOLATION", "reject",
                     f"{symbol}={achieved:.6g} is below the policy minimum {entry.minimum:g}")
            continue
        if entry.maximum is not None and achieved > entry.maximum:
            _finding(plan.findings, "POLICY_MAX_VIOLATION", "reject",
                     f"{symbol}={achieved:.6g} is above the policy maximum {entry.maximum:g}")
            continue
        if obj.range_low is not None and achieved < obj.range_low:
            _finding(plan.findings, "XDF_RANGE_VIOLATION", "reject",
                     f"{symbol}={achieved:.6g} is below the XDF range minimum "
                     f"{obj.range_low:g} {obj.units}")
            continue
        if obj.range_high is not None and achieved > obj.range_high:
            _finding(plan.findings, "XDF_RANGE_VIOLATION", "reject",
                     f"{symbol}={achieved:.6g} is above the XDF range maximum "
                     f"{obj.range_high:g} {obj.units}")
            continue

        absolute_end = calibration_offset + obj.address + (index + 1) * obj.element_size_bytes
        if absolute_end > calibration_offset + covered_end:
            _finding(plan.findings, "OUTSIDE_CRC_COVERAGE", "reject",
                     f"{symbol} at 0x{obj.address + index * obj.element_size_bytes:04X} lies "
                     f"beyond 0x{covered_end - 1:04X}, the end of the CRC-protected "
                     f"calibration region; such a write could never be verified")
            continue
        record = find_record_for_absolute(
            segments_by_record, calibration_offset + obj.address + index * obj.element_size_bytes
        )
        if record is None:
            _finding(plan.findings, "OUTSIDE_CRC_COVERAGE", "reject",
                     f"{symbol} at 0x{obj.address + index * obj.element_size_bytes:04X} is not "
                     f"covered by any CRC record of this image")
            continue

        step = obj.element_size_bytes
        old_raw = int.from_bytes(
            image[calibration_offset + obj.address + index * step:
                  calibration_offset + obj.address + (index + 1) * step],
            "little" if obj.lsb_first else "big",
        )
        old_engineering = obj.to_engineering(old_raw)
        if old_raw == new_raw:
            _finding(plan.findings, "CELLS_ALREADY_MATCH", "info",
                     f"{symbol}[{index}] already holds raw {new_raw} "
                     f"({achieved:.6g} {obj.units}); nothing to write")
            continue

        delta: float | None = None
        if entry.max_delta_percent is not None:
            if old_engineering == 0:
                if achieved != 0:
                    _finding(plan.findings, "DELTA_FROM_ZERO", "warning",
                             f"{symbol} is stock 0; the relative delta rule is skipped for "
                             f"this patch and the absolute policy limits apply.")
            else:
                delta = (achieved - old_engineering) / abs(old_engineering) * 100.0
                if abs(delta) > entry.max_delta_percent:
                    _finding(plan.findings, "DELTA_TOO_LARGE", "reject",
                             f"{symbol}: {old_engineering:.6g} -> {achieved:.6g} is "
                             f"{delta:+.2f}%, beyond the {entry.max_delta_percent:.2f}% limit")
                    continue

        for offset in offsets:
            used_ranges[offset] = symbol
        plan.changes.append(PlannedChange(
            symbol=symbol,
            index=index,
            address=obj.address + index * step,
            byte_offsets=offsets,
            old_raw=old_raw,
            new_raw=new_raw,
            old_engineering=old_engineering,
            new_engineering=achieved,
            requested_engineering=value,
            quantization_error_percent=quantisation,
            delta_percent=delta,
            units=obj.units,
            kind=obj.kind,
            crc_record=record,
        ))

    plan = _check_monotonic_tables(plan, image, calibration_offset, objects, policy)
    if any(finding.severity == "reject" for finding in plan.findings):
        plan.status = "REJECTED"
    elif plan.changes:
        plan.status = "READY"
    else:
        plan.status = "EMPTY"
    return plan


def _check_monotonic_tables(
    plan: PatchPlan,
    image: bytearray,
    calibration_offset: int,
    objects: dict[str, list[XdfObject]],
    policy: Policy,
) -> PatchPlan:
    """Reject table edits that would break a required monotonic curve.

    The MS42 MAF transfer function must be non-decreasing; a monotonicity break
    is a real calibration fault, not a cosmetic issue.
    """
    touched: dict[str, list[int]] = {}
    for change in plan.changes:
        touched.setdefault(change.symbol, []).append(change.index)
    for symbol, indices in touched.items():
        entry = policy.entry(symbol)
        if entry is None:
            continue
        try:
            obj = _resolve_objects(symbol, objects, entry)
        except PatchError:
            continue
        if entry.max_cells_per_patch is not None and len(indices) > entry.max_cells_per_patch:
            _finding(plan.findings, "TOO_MANY_CELLS", "reject",
                     f"{symbol}: {len(indices)} cells changed in one patch, above the "
                     f"limit of {entry.max_cells_per_patch}")
        if not entry.require_monotonic:
            continue
        step = obj.element_size_bytes
        values = []
        for i in range(obj.element_count):
            offset = calibration_offset + obj.address + i * step
            chunk = image[offset:offset + step]
            values.append(int.from_bytes(chunk, "little" if obj.lsb_first else "big"))
        for change in plan.changes:
            if change.symbol == symbol:
                values[change.index] = change.new_raw
        breaks = [i for i in range(1, len(values)) if values[i] < values[i - 1]]
        if breaks:
            _finding(plan.findings, "MONOTONICITY_BROKEN", "reject",
                     f"{symbol}: the curve would stop being non-decreasing at "
                     f"index {breaks[0]} (raw {values[breaks[0] - 1]} -> {values[breaks[0]]})")
    return plan


def apply_plan(
    image: bytearray,
    calibration_offset: int,
    address_base: int,
    plan: PatchPlan,
    objects: dict[str, list[XdfObject]],
) -> tuple[bytearray, list[str]]:
    """Encode every planned change and rebuild the affected CRC records.

    Encoding always goes through the XDF object, so a plan cannot smuggle in a
    byte order or element width that disagrees with the definition.
    """
    if plan.status not in {"READY", "EMPTY"}:
        raise PatchError(f"plan status is {plan.status}; refusing to write")
    output = bytearray(image)
    touched_absolute: set[int] = set()
    for change in plan.changes:
        obj = _resolve_objects(change.symbol, objects, plan.policy_entries.get(change.symbol))
        file_address = calibration_offset + change.address
        size = len(change.byte_offsets)
        if not (
            calibration_offset <= file_address
            and file_address + size <= calibration_offset + CALIBRATION_SIZE
        ):
            raise PatchError(
                f"planned write at 0x{file_address:X} leaves the calibration area"
            )
        if not all(
            calibration_offset <= calibration_offset + offset
            < calibration_offset + CALIBRATION_SIZE
            for offset in change.byte_offsets
        ):
            raise PatchError(
                f"planned write for {change.symbol} leaves the calibration area"
            )
        before = bytes(output[file_address:file_address + size])
        xdf_module.write_element(output, file_address, obj, change.new_raw)
        after = bytes(output[file_address:file_address + size])
        if before != after:
            for offset in change.byte_offsets:
                touched_absolute.add(calibration_offset + offset)
    recomputed = recompute_records(output, address_base, touched_absolute)
    return output, recomputed


def verify_application(
    source: bytes | bytearray,
    output: bytes | bytearray,
    calibration_offset: int,
    address_base: int,
    plan: PatchPlan,
    objects: dict[str, list[XdfObject]],
) -> tuple[list[int], list[Finding]]:
    planned = {
        calibration_offset + offset
        for change in plan.changes
        for offset in change.byte_offsets
    }
    derived_crc = set(crc_storage_offsets(source, address_base))
    actual = {
        index for index, (before, after) in enumerate(zip(source, output)) if before != after
    }
    unplanned = sorted(actual - planned - derived_crc)
    findings: list[Finding] = []
    if unplanned:
        _finding(findings, "UNPLANNED_BYTE_CHANGES", "reject",
                 f"{len(unplanned)} byte(s) changed outside the plan and outside the "
                 f"CRC records, first at 0x{unplanned[0]:X}")
    touched_crc = sorted(actual & derived_crc)
    if touched_crc:
        _finding(findings, "CRC_BYTES_REWRITTEN", "info",
                 f"{len(touched_crc)} CRC byte(s) rewritten in "
                 f"{len(touched_crc) // 2} record(s)")
    # A multi-byte element may legitimately change in only one of its bytes (a
    # 16-bit cell crossing 0x00FF, for example), so the requirement is per element
    # - the written value must be the planned one - not per byte.
    not_written: list[int] = []
    for change in plan.changes:
        obj = _resolve_objects(change.symbol, objects, plan.policy_entries.get(change.symbol))
        offset = calibration_offset + change.address
        size = len(change.byte_offsets)
        written = int.from_bytes(
            output[offset:offset + size], "little" if obj.lsb_first else "big"
        )
        if written != change.new_raw:
            _finding(findings, "VALUE_MISMATCH", "reject",
                     f"{change.symbol}: wrote {written}, expected {change.new_raw}")
        if change.new_raw != change.old_raw and written == change.old_raw:
            not_written.append(offset)
    if not_written:
        _finding(findings, "PLANNED_BYTE_NOT_WRITTEN", "reject",
                 f"{len(not_written)} planned element(s) still hold their old value, "
                 f"first at 0x{not_written[0]:X}")
    ok, errors = verify_records(output, address_base)
    if not ok:
        _finding(findings, "CRC_INVALID_AFTER_PATCH", "reject", "; ".join(errors))
    return unplanned, findings


def run(args: argparse.Namespace) -> int:
    policy = Policy.load(args.policy)
    objects, problems = load_definition(args.xdf)
    try:
        image, offset, image_type, address_base = load_calibration(args.bin)
    except PatchError as exc:
        raise SystemExit(f"error: {exc}")

    requests = json.loads(args.patches.read_text(encoding="utf-8"))
    if isinstance(requests, dict):
        status = requests.get("status")
        if status not in (None, "READY"):
            raise SystemExit(
                f"error: patch file {args.patches} is marked {status}; it was generated "
                "from a request with unresolved intents. Fix the request and regenerate."
            )
        requests = requests.get("patches", [])
    if not isinstance(requests, list):
        raise SystemExit("error: patch file must be a list or an object with a 'patches' list")

    plan = plan_patches(
        image, offset, address_base, image_type, objects, problems, requests, policy
    )
    plan.source = str(args.bin)
    # A patch entry may state the units it knows the object to be in: an XDF
    # units field can be mislabelled. This is display metadata only - encoding
    # still goes through the object.
    stated = {
        (entry.get("symbol"), int(entry.get("index", 0))): entry.get("units")
        for entry in requests
        if isinstance(entry, dict) and entry.get("units")
    }
    for change in plan.changes:
        stated_units = stated.get((change.symbol, change.index))
        if stated_units:
            change.units = stated_units

    if args.out and plan.status == "READY":
        output, recomputed = apply_plan(image, offset, address_base, plan, objects)
        unplanned, findings = verify_application(
            image, output, offset, address_base, plan, objects
        )
        plan.findings.extend(findings)
        failed = any(finding.severity == "reject" for finding in plan.findings)
        if not failed:
            args.out.write_bytes(bytes(output))
        value_offsets = {o for c in plan.changes for o in c.byte_offsets}
        crc_offsets = set(crc_storage_offsets(image, address_base))
        source_base = offset
        changed = {
            index for index, (before, after) in enumerate(zip(image, output)) if before != after
        }
        # Count the planned bytes that really changed: a 16-bit element can move in
        # only one of its two bytes, and reporting the slot count would overstate it.
        value_changed = {
            index for index in changed if index - source_base in value_offsets
        }
        crc_changed = {index for index in changed if index in crc_offsets}
        result = PatchResult(
            status="REJECTED" if failed else ("WRITTEN" if plan.changes else "EMPTY"),
            source=str(args.bin),
            output=None if failed else str(args.out),
            source_sha256=plan.source_sha256,
            output_sha256=None if failed else hashlib.sha256(bytes(output)).hexdigest(),
            changes=plan.changes,
            value_bytes_changed=len(value_changed),
            crc_bytes_changed=len(crc_changed),
            total_bytes_changed=len(changed),
            crc_records_recomputed=len(recomputed),
            crc_valid_after=None if failed else True,
            unplanned_byte_changes=unplanned,
            findings=plan.findings,
            dry_run=False,
        )
    else:
        result = PatchResult(
            status=plan.status,
            source=str(args.bin),
            output=None,
            source_sha256=plan.source_sha256,
            output_sha256=None,
            changes=plan.changes,
            value_bytes_changed=len({o for c in plan.changes for o in c.byte_offsets}),
            crc_bytes_changed=0,
            total_bytes_changed=0,
            crc_records_recomputed=0,
            crc_valid_after=None,
            unplanned_byte_changes=[],
            findings=plan.findings,
            dry_run=True,
        )

    payload = {
        "status": result.status,
        "source": result.source,
        "output": result.output,
        "source_sha256": result.source_sha256,
        "output_sha256": result.output_sha256,
        "image_type": plan.image_type,
        "calibration_offset": plan.calibration_offset,
        "dry_run": result.dry_run,
        "value_bytes_changed": result.value_bytes_changed,
        "crc_bytes_changed": result.crc_bytes_changed,
        "total_bytes_changed": result.total_bytes_changed,
        "crc_records_recomputed": result.crc_records_recomputed,
        # A dry run has no output image, so the records cannot be verified yet:
        # null, not False, so a consumer cannot read it as a broken checksum.
        "crc_valid_after": None if result.dry_run else result.crc_valid_after,
        "unplanned_byte_changes": result.unplanned_byte_changes,
        "findings": [asdict(finding) for finding in result.findings],
        "changes": [
            {
                **asdict(change),
                "byte_offsets": [f"0x{offset:04X}" for offset in change.byte_offsets],
                "address": f"0x{change.address:04X}",
                "file_address": f"0x{offset + change.address:05X}",
            }
            for change in result.changes
        ],
    }
    if args.report:
        args.report.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    if args.json or not args.report:
        print(json.dumps(payload, indent=2))
    else:
        print(
            f"{payload['status']}: {len(payload['changes'])} change(s), "
            f"{payload['value_bytes_changed']} value byte(s), "
            f"{payload['crc_bytes_changed']} CRC byte(s), "
            f"records recomputed: {payload['crc_records_recomputed']}"
        )
        for finding in payload["findings"]:
            print(f"  [{finding['severity']}] {finding['code']}: {finding['message']}")
    return 0 if result.status not in {"REJECTED"} else 2


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bin", type=Path, required=True)
    parser.add_argument("--patches", type=Path, required=True,
                        help="JSON list of {symbol, value, index?} requests")
    parser.add_argument("--out", type=Path, help="write the patched image here")
    parser.add_argument("--report", type=Path, help="write the JSON report here")
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--xdf", type=Path, default=DEFAULT_XDF)
    parser.add_argument("--json", action="store_true", help="print the full JSON report")
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
