#!/usr/bin/env python3
"""Transfer maps from a documented donor calibration (MS43) into an MS42 file.

The MS4X M52TUB25 article recommends copying the DISA maps from an M54 (MS43)
calibration into the M52TU one, and MS43 donor images are in ``MS43/firmware``.
Doing that automatically is only safe when every step is provable, so this
module proves each one:

* **the donor's addressing is derived, not assumed.** The base offset is read
  from the donor XDF header (``BASEOFFSET``) and then *validated*: two known
  objects must read as physically plausible values at that base (a non-decreasing
  MAF curve reaching at least 500 kg/h and an ignition map inside +-90 degCRK).
  A wrong base fails the validation instead of producing garbage that looks like
  a table;
* **every transfer is explicit.** ``MS42/donor/ms43_to_ms42.json`` names the
  source and target symbol, the mode, and why. There is no name matching by
  similarity, so a renamed object cannot be pulled in by accident;
* **the shapes and scales must agree.** Element counts and (rows, columns) must
  match, or the geometry must be declared transposed; the scaler of the two
  objects must be identical. ``c_maf_hys_vim`` is the instructive case: MS43
  uses 10.8941 mg/stk per count where MS42 uses 5.44706, so a donor value there
  would be off by two - the transfer is refused out loud;
* **policy first.** A donor value outside the MS42 policy bounds for the target
  symbol is refused; the patch core then checks the usual quantum and CRC rules.

Every change carries ``donor_ms43:<dataset>:<symbol>`` as its evidence, so a
delivered file can always be traced back to the donor calibration it came from.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from MS42.tools.calibration import (
        DEFAULT_POLICY,
        Policy,
        PatchError,
    )
    from MS42.tools.xdf import XdfError, get_object, load_definition, read_engineering
except ModuleNotFoundError:  # Direct execution from MS42/tools.
    from calibration import (  # type: ignore[no-redef]
        DEFAULT_POLICY,
        Policy,
        PatchError,
    )
    from xdf import XdfError, get_object, load_definition, read_engineering  # type: ignore[no-redef]

ROOT = Path(__file__).parents[1]
DEFAULT_MAP = ROOT / "donor" / "ms43_to_ms42.json"
DEFAULT_DONOR_XDF = ROOT.parent / "MS43" / "definitions" / "Siemens_MS43_430069_Extended_Load_512K.xdf"
DEFAULT_DONOR_BIN = (ROOT.parent / "MS43" / "firmware"
                     / "Siemens_MS43_MS430069_E46_M54B30_EU4_LHD.bin")

#: Base offset is validated with these objects: (symbol, predicate description).
VALIDATORS = (
    ("id_maf_tab__v_maf_1__v_maf_2", "non-decreasing, peak >= 500 kg/h, min >= 0"),
    ("ip_iga_ron_98_pl_ivvt__n__maf", "every angle within -90..90 degCRK"),
)
#: Relative tolerance when comparing the two definitions' scale factors. The XDFs store
#: different numbers of decimals (MS42 0.468627 against MS43 0.46862745), so exact
#: equality would refuse a legitimate transfer, while a factor-of-two difference
#: (c_maf_hys_vim: 5.44706 against 10.8941) still fails by a wide margin.
SCALE_TOLERANCE = 1e-4


class DonorError(PatchError):
    """Raised when a donor calibration cannot be used safely."""


def transposed_order(rows: int, columns: int) -> list[int]:
    """Flat indices of a (rows, columns) grid read in its (columns, rows) transpose."""
    return [row * columns + column for column in range(columns) for row in range(rows)]


@dataclass
class Transfer:
    ms42_symbol: str
    ms43_symbol: str
    mode: str                 # "direct" | "reference"
    note: str
    orientation: str = "same"
    sources: tuple[str, ...] = ()


@dataclass
class Donor:
    path: str
    sha256: str
    family: str
    dataset_id: str | None
    osid: str | None
    calibration_base: int
    xdf: str
    objects: dict[str, list]
    image: bytes
    problems: list[str] = field(default_factory=list)

    @property
    def label(self) -> str:
        return self.dataset_id or Path(self.path).name

    def values(self, symbol: str, orientation: str = "same") -> list[float]:
        obj = get_object(self.objects, symbol)
        values = read_engineering(self.image[self.calibration_base:], obj)
        if orientation == "same":
            return values
        if orientation != "transposed":
            raise DonorError(f"{symbol}: unsupported orientation {orientation!r}")
        # Row-major flatten of the transpose: only valid when the target shape is
        # exactly the donor shape flipped, which inspect_transfer already proved.
        return [values[index] for index in transposed_order(obj.rows, obj.columns)]


def _parse_base(xdf_path: Path) -> int:
    text = xdf_path.read_text(encoding="utf-8", errors="replace")
    match = re.search(r"<BASEOFFSET\s+offset=\"(0x[0-9A-Fa-f]+|\d+)\"", text)
    if not match:
        raise DonorError(f"{xdf_path}: no BASEOFFSET in the definition header")
    return int(match.group(1), 0)


def _ascii_id(image: bytes, base: int, length: int = 8) -> str | None:
    chunk = image[base:base + length]
    if len(chunk) != length or any(value < 0x20 or value > 0x7E for value in chunk):
        return None
    return chunk.decode("ascii")


def load_donor(bin_path: Path = DEFAULT_DONOR_BIN,
               xdf_path: Path = DEFAULT_DONOR_XDF) -> Donor:
    """Load and *validate* a donor calibration before anything is transferred."""
    image = bin_path.read_bytes()
    objects, problems = load_definition(xdf_path)
    base = _parse_base(xdf_path)

    # The calibration area is 32 KiB; the base plus that window has to exist for
    # the validator below to run at all.
    if base + 0x8000 > len(image):
        raise DonorError(
            f"{bin_path}: the definition declares base 0x{base:X}, which does not fit a "
            f"{len(image)} byte image (a 32 KiB calibration area is needed at that base)"
        )

    failures: list[str] = []
    for symbol, expectation in VALIDATORS:
        try:
            values = read_engineering(image[base:], get_object(objects, symbol))
        except XdfError as exc:
            failures.append(f"{symbol}: {exc}")
            continue
        if symbol == "id_maf_tab__v_maf_1__v_maf_2":
            monotonic = all(right >= left for left, right in zip(values, values[1:]))
            if not monotonic or max(values) < 500 or min(values) < 0:
                failures.append(f"{symbol}: base 0x{base:X} gives "
                                f"{min(values):.4g}..{max(values):.4g} kg/h, not a MAF curve")
        else:
            if not all(-90.0 <= value <= 90.0 for value in values):
                failures.append(f"{symbol}: base 0x{base:X} gives angles outside -90..90 degCRK")
    if failures:
        raise DonorError(
            f"{bin_path} with {xdf_path.name}: the declared base offset 0x{base:X} does not "
            f"produce physically plausible values ({'; '.join(failures)}). Refusing to guess."
        )

    header = image[base:base + 64]
    printable = bytes(value for value in header if 0x20 <= value < 0x7E)
    dataset = printable[:8].decode("ascii") if len(printable) >= 8 else None
    # The calibration header reads e.g. "C769Y5484300690111143000690100001114...",
    # so the software version is the first six digit run inside the header.
    # The header reads e.g. "C769Y5484300690111143000690100001114300069010000": the
    # dataset code (430069) sits after the 8-character dataset id. Anything else is
    # not guessed - the version stays None and the label falls back to the file name.
    marker = printable.find(b"430069")
    osid = printable[marker:marker + 6].decode("ascii") if marker >= 0 else None

    return Donor(
        path=str(bin_path),
        sha256=hashlib.sha256(image).hexdigest(),
        family="MS43",
        dataset_id=dataset,
        osid=osid,
        calibration_base=base,
        xdf=str(xdf_path),
        objects=objects,
        image=image,
        problems=problems,
    )


def load_map(path: Path = DEFAULT_MAP) -> list[Transfer]:
    data = json.loads(path.read_text(encoding="utf-8"))
    if int(data.get("schema", 0)) != 1:
        raise DonorError(f"{path}: unsupported schema {data.get('schema')!r}")
    transfers: list[Transfer] = []
    for raw in data.get("transfers", []):
        mode = str(raw.get("mode", ""))
        if mode not in ("direct", "reference"):
            raise DonorError(f"{path}: {raw.get('ms42_symbol')!r}: mode must be "
                             f"'direct' or 'reference', got {mode!r}")
        note = str(raw.get("note", "")).strip()
        if not note:
            raise DonorError(f"{path}: {raw.get('ms42_symbol')!r}: a note is required - the "
                             f"mapping has to say why the transfer is valid")
        transfers.append(Transfer(
            ms42_symbol=str(raw.get("ms42_symbol", "")),
            ms43_symbol=str(raw.get("ms43_symbol", "")),
            mode=mode,
            note=note,
            orientation=str(raw.get("orientation", "same")),
            sources=tuple(str(value) for value in raw.get("sources", [])),
        ))
    return transfers


def inspect_transfer(transfer: Transfer, donor: Donor, target_objects: dict[str, list],
                     policy: Policy) -> dict[str, Any]:
    """Check one mapping and report what would be written (no change is made here)."""
    report: dict[str, Any] = {
        "ms42_symbol": transfer.ms42_symbol,
        "ms43_symbol": transfer.ms43_symbol,
        "mode": transfer.mode,
        "ok": False,
        "problems": [],
        "note": transfer.note,
    }
    try:
        target = get_object(target_objects, transfer.ms42_symbol)
    except XdfError as exc:
        report["problems"].append(str(exc))
        return report
    try:
        source = get_object(donor.objects, transfer.ms43_symbol)
    except XdfError as exc:
        report["problems"].append(f"donor: {exc}")
        return report

    if (target.rows, target.columns) != (source.rows, source.columns):
        transposed = (target.rows, target.columns) == (source.columns, source.rows)
        if not transposed or transfer.orientation != "transposed":
            report["problems"].append(
                f"shape mismatch: target {target.rows}x{target.columns}, donor "
                f"{source.rows}x{source.columns}"
                + (" (a transpose must be declared in the mapping)" if transposed else "")
            )
            return report
    a_close = abs(target.scaler.a - source.scaler.a) <= SCALE_TOLERANCE * max(
        abs(target.scaler.a), abs(source.scaler.a), 1e-12)
    b_close = abs(target.scaler.b - source.scaler.b) <= SCALE_TOLERANCE * max(
        abs(target.scaler.b), abs(source.scaler.b), 1e-12)
    if not a_close or not b_close:
        report["problems"].append(
            f"scale mismatch: target {target.scaler.a!r}x + {target.scaler.b!r}, donor "
            f"{source.scaler.a!r}x + {source.scaler.b!r}; a donor value cannot be carried across "
            f"a different scale"
        )
        return report
    if target.lsb_first != source.lsb_first:
        report["problems"].append(
            f"byte order mismatch: target lsb_first={target.lsb_first}, donor "
            f"lsb_first={source.lsb_first}"
        )
        return report
    if target.element_size_bits != source.element_size_bits:
        report["problems"].append(
            f"element size mismatch: target {target.element_size_bits}b, donor "
            f"{source.element_size_bits}b"
        )
        return report

    entry = policy.entry(transfer.ms42_symbol)
    if entry is None:
        report["problems"].append(f"{transfer.ms42_symbol} is not in the patch policy")
        return report

    values = donor.values(transfer.ms43_symbol, transfer.orientation)
    if entry.minimum is not None and min(values) < entry.minimum - 1e-9:
        report["problems"].append(f"donor minimum {min(values):.6g} is below the policy minimum "
                                  f"{entry.minimum:g}")
    if entry.maximum is not None and max(values) > entry.maximum + 1e-9:
        report["problems"].append(f"donor maximum {max(values):.6g} is above the policy maximum "
                                  f"{entry.maximum:g}")
    report.update({
        "ok": not report["problems"],
        "rows": source.rows,
        "columns": source.columns,
        "scale": source.scaler.a,
        "units": target.units,
        "min": round(min(values), 6),
        "max": round(max(values), 6),
        "points": len(values),
        "sources": list(transfer.sources),
    })
    return report


def plan_transfer(donor: Donor, target_objects: dict[str, list], image: bytes, offset: int,
                  policy: Policy, *, map_path: Path = DEFAULT_MAP,
                  include_references: bool = False) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return (changes, reports) for every applicable mapping.

    ``direct`` mappings are applied whenever a donor is supplied; ``reference``
    mappings only when the caller explicitly opts in (a reference value is
    plausible for the donor's engine, not a measurement of this one).
    """
    if donor is None:
        raise DonorError("no donor calibration was given: there is nothing to transfer")
    transfers = load_map(map_path)
    calibration = bytes(image[offset:offset + 0x8000])
    changes: list[dict[str, Any]] = []
    reports: list[dict[str, Any]] = []
    for transfer in transfers:
        report = inspect_transfer(transfer, donor, target_objects, policy)
        report["applied"] = False
        if not report["ok"]:
            reports.append(report)
            continue
        if transfer.mode == "reference" and not include_references:
            report["problems"].append("reference value: needs explicit opt-in "
                                      "(hardware.use_donor_references)")
            report["ok"] = False
            reports.append(report)
            continue
        target = get_object(target_objects, transfer.ms42_symbol)
        values = donor.values(transfer.ms43_symbol, transfer.orientation)
        evidence = f"donor_ms43:{donor.label}:{transfer.ms43_symbol}"
        reason = (f"перенос карты с донорской калибровки MS43 {donor.label} "
                  f"({Path(donor.path).name})")
        for index, value in enumerate(values):
            changes.append({
                "symbol": transfer.ms42_symbol,
                "index": index,
                "value": value,
                "evidence": evidence,
                "reason": reason,
                "requires_validation": True,
                "validation_hint": ("дорожная проверка: поведение заслонки/нагрузки на переходных "
                                    "режимах и отсутствие ошибок по DISA"),
                "units": target.units,
                "donor_symbol": transfer.ms43_symbol,
            })
        report["applied"] = True
        reports.append(report)
    return changes, reports


def donor_payload(donor: Donor, reports: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "donor": {
            "path": donor.path,
            "sha256": donor.sha256,
            "family": donor.family,
            "dataset_id": donor.dataset_id,
            "osid": donor.osid,
            "calibration_base": donor.calibration_base,
            "xdf": donor.xdf,
        },
        "transfers": reports,
        "applied": [report["ms42_symbol"] for report in reports if report.get("applied")],
        "refused": [report["ms42_symbol"] for report in reports if not report.get("applied")],
    }


def run(args: argparse.Namespace) -> int:
    donor = load_donor(args.donor_bin, args.donor_xdf)
    policy = Policy.load(args.policy)
    target_objects, target_problems = load_definition(args.xdf)
    target = args.bin.read_bytes() if args.bin else None
    if target is None:
        payload = donor_payload(donor, [
            inspect_transfer(transfer, donor, target_objects, policy)
            for transfer in load_map(args.map)
        ])
        payload["donor"]["problems"] = donor.problems
        payload["target_xdf_problems"] = target_problems
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return 0 if all(report["ok"] for report in payload["transfers"]) else 1

    try:
        from MS42.tools.analyze_firmware import analyze  # local import: CLI only
    except ModuleNotFoundError:
        from analyze_firmware import analyze  # type: ignore[no-redef]
    analysis = analyze(args.bin)
    if not analysis.supported or analysis.calibration_offset is None:
        raise SystemExit(f"error: {args.bin} is not a supported MS42 image")
    changes, reports = plan_transfer(donor, target_objects, target,
                                     analysis.calibration_offset, policy,
                                     map_path=args.map,
                                     include_references=args.references)
    payload = donor_payload(donor, reports)
    payload["changes"] = changes
    payload["change_count"] = len(changes)
    print(json.dumps(payload, indent=2, ensure_ascii=False))
    return 0 if changes or not any(report["ok"] for report in reports) else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--donor-bin", type=Path, default=DEFAULT_DONOR_BIN)
    parser.add_argument("--donor-xdf", type=Path, default=DEFAULT_DONOR_XDF)
    parser.add_argument("--xdf", type=Path, default=None,
                        help="target (MS42) definition; default is the 512K one")
    parser.add_argument("--bin", type=Path, default=None,
                        help="optional MS42 image to plan the transfer against")
    parser.add_argument("--map", type=Path, default=DEFAULT_MAP)
    parser.add_argument("--policy", type=Path, default=DEFAULT_POLICY)
    parser.add_argument("--references", action="store_true",
                        help="apply 'reference' mappings too (needs an explicit decision)")
    args = parser.parse_args()
    if args.xdf is None:
        args.xdf = ROOT / "definitions" / "Siemens_MS42_0110C6_ENG_512K_v1.3.xdf"
    return run(args)


if __name__ == "__main__":
    raise SystemExit(main())
