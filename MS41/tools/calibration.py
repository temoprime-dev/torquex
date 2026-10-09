#!/usr/bin/env python3
"""Patch MS41 calibrations inside a strict allowlist, with a full diff report.

This is the first component that can damage a flash image, so it is built the
other way round from a normal editor:

* default deny - a symbol is patchable only when the policy file lists it;
* only the calibration block is writable, never program code;
* the source image must pass its own CRC chain before anything is planned;
* the calibration chain must be valid again after patching;
* the set of changed bytes must equal the planned set exactly, and nothing else;
* a plan is refused when a value cannot be represented precisely enough.

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
    from MS41.tools.analyze_firmware import (
        CALIBRATION_OFFSET,
        PARTIAL_SIZE,
        _crc16,
        analyze,
    )
    from MS41.tools import xdf as xdf_module
    from MS41.tools.xdf import XdfError, XdfObject, load_definition
except ModuleNotFoundError:  # Direct execution from MS41/tools.
    from analyze_firmware import CALIBRATION_OFFSET, PARTIAL_SIZE, _crc16, analyze
    import xdf as xdf_module
    from xdf import XdfError, XdfObject, load_definition

ROOT = Path(__file__).parents[1]
DEFAULT_XDF = ROOT / "definitions" / "Siemens_MS41_1429861_ENG_24K_v1.0.xdf"
DEFAULT_POLICY = ROOT / "policies" / "stage_policy.json"

#: Уровень доверия к выпущенному файлу (задание §6): байты и суммы проверены,
#: на живой машине файл не проверялся.
TRUST_LEVEL_STATEMENT = (
    "bytes+crc verified: байты и контрольные суммы сверены; "
    "field verified: на реальном ЭБУ не проверялось"
)
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


@dataclass
class PatchPlan:
    status: str
    source: str
    source_sha256: str
    image_type: str | None
    calibration_offset: int | None
    findings: list[Finding] = field(default_factory=list)
    changes: list[PlannedChange] = field(default_factory=list)
    policy_version: int = 1
    objects_available: int = 0
    unrepresentable_objects: list[str] = field(default_factory=list)


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
    crc_blocks_recomputed: int
    crc_valid_after: bool
    unplanned_byte_changes: list[int]
    findings: list[Finding]
    dry_run: bool


def _finding(findings: list[Finding], code: str, severity: str, message: str) -> None:
    findings.append(Finding(code, severity, message))


def load_calibration(path: Path) -> tuple[bytearray, int, str]:
    """Return (whole image, calibration offset, image type) for a supported BIN."""
    analysis = analyze(path)
    if not analysis.supported:
        raise PatchError(
            f"{path} is not a supported MS41 image: {'; '.join(analysis.reasons)}"
        )
    assert analysis.calibration_offset is not None
    return bytearray(path.read_bytes()), analysis.calibration_offset, str(analysis.image_type)


def verify_crc_chain(calibration: bytes | bytearray) -> tuple[bool, int, list[str]]:
    if len(calibration) < PARTIAL_SIZE:
        return False, 0, ["calibration block is shorter than 24K"]
    initial = int.from_bytes(calibration[0x0E:0x10], "big")
    cursor = 0
    blocks = 0
    errors: list[str] = []
    while cursor + 2 <= PARTIAL_SIZE:
        checksum_address = int.from_bytes(calibration[cursor:cursor + 2], "little")
        if checksum_address == 0xFFFF:
            break
        if checksum_address <= cursor or checksum_address + 2 > PARTIAL_SIZE:
            errors.append(f"invalid CRC pointer 0x{checksum_address:04X} at 0x{cursor:04X}")
            break
        recorded = int.from_bytes(calibration[checksum_address:checksum_address + 2], "little")
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


def crc_covered_end(calibration: bytes | bytearray) -> int:
    """End of the region the CRC chain actually protects.

    The chain covers ``0x0000`` up to the address of the last checksum word; the
    checksum words themselves and everything after the final one are outside it.
    MS41 calibrations are 0x6000 long but only the first 0x2D4A bytes are
    protected, so a write past this point would survive silently and unsigned.
    """
    if len(calibration) < PARTIAL_SIZE:
        raise PatchError("calibration block is shorter than 24K")
    cursor = 0
    last_covered = 0
    while cursor + 2 <= PARTIAL_SIZE:
        checksum_address = int.from_bytes(calibration[cursor:cursor + 2], "little")
        if checksum_address == 0xFFFF:
            break
        if checksum_address <= cursor or checksum_address + 2 > PARTIAL_SIZE:
            raise PatchError(f"invalid CRC pointer 0x{checksum_address:04X} at 0x{cursor:04X}")
        last_covered = checksum_address
        cursor = checksum_address + 2
    else:
        raise PatchError("CRC chain has no terminator")
    if last_covered == 0:
        raise PatchError("no CRC blocks found")
    return last_covered


def crc_storage_offsets(calibration: bytes | bytearray) -> list[int]:
    """Offsets of every stored CRC word in the chain.

    These bytes are legitimately rewritten when any covered byte changes, so the
    application check must treat them as expected rather than unplanned.
    """
    initial = int.from_bytes(calibration[0x0E:0x10], "big")
    del initial  # seeding is only needed for value computation
    offsets: list[int] = []
    cursor = 0
    while cursor + 2 <= PARTIAL_SIZE:
        checksum_address = int.from_bytes(calibration[cursor:cursor + 2], "little")
        if checksum_address == 0xFFFF:
            break
        if checksum_address <= cursor or checksum_address + 2 > PARTIAL_SIZE:
            break
        offsets.extend((checksum_address, checksum_address + 1))
        cursor = checksum_address + 2
    return offsets


def recompute_crc(calibration: bytearray) -> int:
    """Rebuild the whole calibration CRC chain in place; returns the block count."""
    initial = int.from_bytes(calibration[0x0E:0x10], "big")
    cursor = 0
    blocks = 0
    while cursor + 2 <= PARTIAL_SIZE:
        checksum_address = int.from_bytes(calibration[cursor:cursor + 2], "little")
        if checksum_address == 0xFFFF:
            break
        if checksum_address <= cursor or checksum_address + 2 > PARTIAL_SIZE:
            raise PatchError(f"invalid CRC pointer 0x{checksum_address:04X} at 0x{cursor:04X}")
        crc = _crc16(calibration[cursor:checksum_address], initial)
        calibration[checksum_address:checksum_address + 2] = crc.to_bytes(2, "little")
        cursor = checksum_address + 2
        blocks += 1
    if blocks == 0:
        raise PatchError("no CRC blocks found")
    return blocks


def _resolve_objects(title: str, objects: dict[str, list[XdfObject]]) -> XdfObject:
    matches = objects.get(title)
    if not matches:
        raise PatchError(f"{title!r} is not a known XDF object")
    if len(matches) > 1:
        raise PatchError(
            f"{title!r} has {len(matches)} definitions; patch it by its explicit form"
        )
    return matches[0]


def _quantization_error_percent(requested: float, achieved: float) -> float:
    scale = max(abs(requested), 1e-9)
    return abs(achieved - requested) / scale * 100.0


def plan_patches(
    image: bytearray,
    calibration_offset: int,
    image_type: str,
    objects: dict[str, list[XdfObject]],
    problems: list[str],
    requests: list[dict[str, Any]],
    policy: Policy,
) -> PatchPlan:
    calibration = image[calibration_offset:calibration_offset + PARTIAL_SIZE]
    covered_end = crc_covered_end(calibration)
    source_hash = hashlib.sha256(bytes(image)).hexdigest()
    plan = PatchPlan(
        status="PLANNED",
        source="",
        source_sha256=source_hash,
        image_type=image_type,
        calibration_offset=calibration_offset,
        policy_version=policy.version,
        objects_available=sum(len(value) for value in objects.values()),
        unrepresentable_objects=problems,
    )
    ok, blocks, errors = verify_crc_chain(calibration)
    if not ok:
        _finding(plan.findings, "SOURCE_CRC_INVALID", "reject",
                 "Source calibration CRC chain is invalid: " + "; ".join(errors))
    if problems:
        _finding(plan.findings, "XDF_OBJECTS_UNREPRESENTABLE", "warning",
                 f"{len(problems)} XDF object(s) cannot be represented and are not patchable.")

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
        try:
            obj = _resolve_objects(symbol, objects)
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
        if quantisation > policy.max_quantization_error_percent:
            _finding(plan.findings, "QUANTIZATION_TOO_COARSE", "reject",
                     f"{symbol}={value} resolves to {achieved:.6g} "
                     f"({quantisation:.2f}% away), beyond the "
                     f"{policy.max_quantization_error_percent:.2f}% policy limit")
            continue
        if entry.minimum is not None and achieved < entry.minimum:
            _finding(plan.findings, "POLICY_MIN_VIOLATION", "reject",
                     f"{symbol}={achieved:.6g} is below the policy minimum {entry.minimum:g}")
            continue
        if obj.address + index * obj.element_size_bytes + obj.element_size_bytes > covered_end:
            _finding(plan.findings, "OUTSIDE_CRC_COVERAGE", "reject",
                     f"{symbol} at 0x{obj.address + index * obj.element_size_bytes:04X} lies "
                     f"beyond 0x{covered_end:04X}, the end of the CRC-protected region; "
                     f"such a write could never be verified")
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

        step = obj.element_size_bytes
        old_raw = int.from_bytes(
            calibration[obj.address + index * step:obj.address + (index + 1) * step],
            "little" if obj.lsb_first else "big",
        )
        old_engineering = obj.to_engineering(old_raw)

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
        ))

    plan = _check_monotonic_tables(plan, calibration, objects, policy, requests)
    if any(finding.severity == "reject" for finding in plan.findings):
        plan.status = "REJECTED"
    elif plan.changes:
        plan.status = "READY"
    else:
        plan.status = "EMPTY"
    return plan


def _check_monotonic_tables(
    plan: PatchPlan,
    calibration: bytes | bytearray,
    objects: dict[str, list[XdfObject]],
    policy: Policy,
    requests: list[dict[str, Any]],
) -> PatchPlan:
    """Reject table edits that would break a required monotonic curve.

    The MS41 MAF transfer function must be non-decreasing; a monotonicity break
    is a real calibration fault, not a cosmetic issue.
    """
    touched: dict[str, list[int]] = {}
    for change in plan.changes:
        touched.setdefault(change.symbol, []).append(change.index)
    for symbol, indices in touched.items():
        entry = policy.entry(symbol)
        obj = next((o for o in objects.get(symbol, [])), None)
        if entry is None or obj is None:
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
            offset = obj.address + i * step
            raw = int.from_bytes(calibration[offset:offset + step],
                                 "little" if obj.lsb_first else "big")
            values.append(raw)
        for change in plan.changes:
            if change.symbol == symbol:
                values[change.index] = change.new_raw
        breaks = [
            i for i in range(1, len(values)) if values[i] < values[i - 1]
        ]
        if breaks:
            _finding(plan.findings, "MONOTONICITY_BROKEN", "reject",
                     f"{symbol}: the curve would stop being non-decreasing at "
                     f"index {breaks[0]} (raw {values[breaks[0] - 1]} -> {values[breaks[0]]})")
    return plan


def apply_plan(
    image: bytearray,
    calibration_offset: int,
    plan: PatchPlan,
    objects: dict[str, list[XdfObject]],
) -> tuple[bytearray, int]:
    """Encode every planned change and rebuild the CRC chain.

    Encoding always goes through the XDF object, so a plan cannot smuggle in a
    byte order or element width that disagrees with the definition.
    """
    if plan.status not in {"READY", "EMPTY"}:
        raise PatchError(f"plan status is {plan.status}; refusing to write")
    output = bytearray(image)
    calibration = bytearray(output[calibration_offset:calibration_offset + PARTIAL_SIZE])
    for change in plan.changes:
        for offset in change.byte_offsets:
            if not 0 <= offset < PARTIAL_SIZE:
                raise PatchError(f"planned write at 0x{offset:X} leaves the calibration block")
        obj = _resolve_objects(change.symbol, objects)
        xdf_module.write_element(calibration, change.address, obj, change.new_raw)
    blocks = recompute_crc(calibration)
    output[calibration_offset:calibration_offset + PARTIAL_SIZE] = calibration
    return output, blocks


def verify_application(
    source: bytes | bytearray,
    output: bytes | bytearray,
    calibration_offset: int,
    plan: PatchPlan,
    objects: dict[str, list[XdfObject]],
) -> tuple[list[int], list[Finding]]:
    # Plan offsets are calibration-relative; file diffs are absolute.
    planned = {
        calibration_offset + offset
        for change in plan.changes
        for offset in change.byte_offsets
    }
    calibration_before = source[
        calibration_offset:calibration_offset + PARTIAL_SIZE
    ]
    derived_crc = {
        calibration_offset + offset for offset in crc_storage_offsets(calibration_before)
    }
    actual = {
        index for index, (before, after) in enumerate(zip(source, output)) if before != after
    }
    unplanned = sorted(actual - planned - derived_crc)
    findings: list[Finding] = []
    if unplanned:
        _finding(findings, "UNPLANNED_BYTE_CHANGES", "reject",
                 f"{len(unplanned)} byte(s) changed outside the plan and outside the "
                 f"CRC chain, first at 0x{unplanned[0]:X}")
    touched_crc = sorted(actual & derived_crc)
    if touched_crc:
        _finding(findings, "CRC_BYTES_REWRITTEN", "info",
                 f"{len(touched_crc)} CRC byte(s) rewritten as expected in "
                 f"{len(touched_crc) // 2} chain location(s)")
    # A multi-byte element may legitimately change in only one of its bytes (a
    # 16-bit cell crossing 0x00FF, for example), so the requirement is per element
    # - the written value must be the planned one - not per byte.
    not_written: list[int] = []
    for change in plan.changes:
        obj = _resolve_objects(change.symbol, objects)
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
    calibration = output[
        calibration_offset:calibration_offset + PARTIAL_SIZE
    ]
    ok, _, errors = verify_crc_chain(calibration)
    if not ok:
        _finding(findings, "CRC_INVALID_AFTER_PATCH", "reject", "; ".join(errors))
    return unplanned, findings


def run(args: argparse.Namespace) -> int:
    policy = Policy.load(args.policy)
    objects, problems = load_definition(args.xdf)
    try:
        image, offset, image_type = load_calibration(args.bin)
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

    plan = plan_patches(image, offset, image_type, objects, problems, requests, policy)
    plan.source = str(args.bin)
    # A patch entry may state the units it knows the object to be in: an XDF
    # units field can be mislabelled (the VANOS threshold table says degrees C
    # while its values are mg/stroke). This is display metadata only - encoding
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
        output, blocks = apply_plan(image, offset, plan, objects)
        unplanned, findings = verify_application(image, output, offset, plan, objects)
        plan.findings.extend(findings)
        failed = any(finding.severity == "reject" for finding in plan.findings)
        if not failed:
            args.out.write_bytes(bytes(output))
        value_offsets = {o for c in plan.changes for o in c.byte_offsets}
        base = offset
        crc_offsets = set(crc_storage_offsets(bytes(image)[base:base + PARTIAL_SIZE]))
        changed = {
            index for index, (before, after) in enumerate(zip(image, output)) if before != after
        }
        # Count the planned bytes that really changed: a 16-bit element can move in
        # only one of its two bytes, and reporting the slot count would overstate it.
        value_changed = {
            index - base for index in changed if index - base in value_offsets
        }
        crc_changed = {index - base for index in changed if index - base in crc_offsets}
        result = PatchResult(
            status="REJECTED" if failed else "WRITTEN",
            source=str(args.bin),
            output=None if failed else str(args.out),
            source_sha256=plan.source_sha256,
            output_sha256=None if failed else hashlib.sha256(bytes(output)).hexdigest(),
            changes=plan.changes,
            value_bytes_changed=len(value_changed),
            crc_bytes_changed=len(crc_changed),
            total_bytes_changed=len(changed),
            crc_blocks_recomputed=blocks,
            crc_valid_after=not failed,
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
            crc_blocks_recomputed=0,
            crc_valid_after=False,
            unplanned_byte_changes=[],
            findings=plan.findings,
            dry_run=True,
        )

    payload = {
        "trust_level": TRUST_LEVEL_STATEMENT,
        "status": result.status,
        "source": result.source,
        "output": result.output,
        "source_sha256": result.source_sha256,
        "output_sha256": result.output_sha256,
        "dry_run": result.dry_run,
        "value_bytes_changed": result.value_bytes_changed,
        "crc_bytes_changed": result.crc_bytes_changed,
        "total_bytes_changed": result.total_bytes_changed,
        "crc_blocks_recomputed": result.crc_blocks_recomputed,
        "crc_covered_end": "0x%04X" % crc_covered_end(bytes(image)[offset:offset + PARTIAL_SIZE]),
        # A dry run has no output image, so the chain cannot be verified yet:
        # null, not False, so a consumer cannot read it as a broken checksum.
        "crc_valid_after": (None if result.dry_run else result.crc_valid_after),
        "unplanned_byte_changes": result.unplanned_byte_changes,
        "findings": [asdict(finding) for finding in result.findings],
        "changes": [
            {
                **asdict(change),
                "byte_offsets": [f"0x{offset:04X}" for offset in change.byte_offsets],
                "address": f"0x{change.address:04X}",
            }
            for change in result.changes
        ],
    }
    if args.report:
        args.report.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"{result.status} {args.bin} -> {result.output or '(no output)'}")
        for finding in result.findings:
            print(f"  [{finding.severity.upper()}] {finding.code}: {finding.message}")
        for change in result.changes:
            print(
                f"  {change.symbol}[{change.index}] @0x{change.address:04X}: "
                f"{change.old_engineering:.6g} -> {change.new_engineering:.6g} "
                f"{change.units} (raw {change.old_raw} -> {change.new_raw})"
            )
        if result.status == "WRITTEN":
            print(f"  value bytes: {result.value_bytes_changed}, "
                  f"CRC bytes: {result.crc_bytes_changed}, "
                  f"total: {result.total_bytes_changed}, "
                  f"CRC blocks recomputed: {result.crc_blocks_recomputed}, "
                  + ("crc valid: not written (dry run)" if result.dry_run
                     else f"crc valid: {result.crc_valid_after}"))
    return 0 if result.status in {"WRITTEN", "READY", "EMPTY"} else 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bin", type=Path, required=True, help="24K or 256K MS41 image")
    parser.add_argument("--patches", type=Path, required=True, help="JSON patch request")
    parser.add_argument("--out", type=Path, help="write the patched image here")
    parser.add_argument("--report", type=Path, help="write the JSON diff report here")
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--xdf", type=Path, default=DEFAULT_XDF)
    parser.add_argument("--json", action="store_true")
    return run(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
