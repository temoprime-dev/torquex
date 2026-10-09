#!/usr/bin/env python3
"""Run a conservative Stage 0 readiness check for Siemens MS41 tuning.

Binary integrity is checked directly. Vehicle health is supplied as JSON because
it cannot be inferred from a BIN. Missing evidence never becomes an implicit
pass.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

try:
    from MS41.tools.analyze_firmware import analyze
except ModuleNotFoundError:  # Direct execution from MS41/tools.
    from analyze_firmware import analyze

DEFAULT_MANIFEST = Path(__file__).parents[1] / "checksums" / "FIRMWARE_SHA256SUMS.txt"
REQUIRED_CONFIRMATIONS = (
    "no_misfire",
    "no_vacuum_leak",
    "fuel_pressure_verified",
    "maf_ok",
    "lambda_sensors_ok",
    "iat_ect_ok",
    "vanos_ok",
    "knock_sensors_ok",
    "stock_backup_saved",
)


@dataclass(frozen=True)
class Finding:
    code: str
    severity: str
    message: str


@dataclass(frozen=True)
class HealthReport:
    status: str
    binary: dict[str, Any]
    stock_provenance: str
    evidence_complete: bool
    findings: list[Finding]
    policy: dict[str, float]


def load_manifest(path: Path) -> dict[str, str]:
    entries: dict[str, str] = {}
    if not path.exists():
        return entries
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(maxsplit=1)
        if len(parts) == 2 and len(parts[0]) == 64:
            entries[parts[0].lower()] = parts[1].lstrip(" *")
    return entries


def _number(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def evaluate(
    bin_path: Path,
    evidence: dict[str, Any] | None = None,
    manifest_path: Path = DEFAULT_MANIFEST,
    trim_warning: float = 10.0,
    trim_reject: float = 15.0,
) -> HealthReport:
    if trim_warning < 0 or trim_reject <= trim_warning:
        raise ValueError("trim thresholds must satisfy 0 <= warning < reject")

    analysis = analyze(bin_path)
    manifest = load_manifest(manifest_path)
    known_name = manifest.get(analysis.sha256.lower())
    findings: list[Finding] = []

    if not analysis.supported:
        findings.append(Finding("BIN_UNSUPPORTED", "reject", "; ".join(analysis.reasons)))
    elif known_name:
        findings.append(Finding("BIN_KNOWN", "info", f"SHA-256 matches fixture {known_name}"))
    else:
        findings.append(Finding(
            "BIN_PROVENANCE_UNKNOWN",
            "warning",
            "Binary structure and CRC are valid, but SHA-256 is not in the stock manifest; "
            "it may already be modified.",
        ))

    if evidence is None:
        findings.append(Finding(
            "EVIDENCE_MISSING", "warning",
            "Vehicle evidence JSON was not provided; binary integrity alone cannot prove readiness.",
        ))
        evidence = {}

    confirmations = evidence.get("confirmations")
    if not isinstance(confirmations, dict):
        confirmations = {}
    for field in REQUIRED_CONFIRMATIONS:
        value = confirmations.get(field)
        if value is not True:
            severity = "reject" if value is False else "warning"
            wording = "failed" if value is False else "missing"
            findings.append(Finding(
                f"CONFIRMATION_{field.upper()}", severity,
                f"Required Stage 0 confirmation is {wording}: {field}",
            ))

    dtcs = evidence.get("dtcs")
    if not isinstance(dtcs, dict):
        dtcs = {}
    for category in ("active", "pending"):
        values = dtcs.get(category)
        if values is None:
            findings.append(Finding(
                f"DTC_{category.upper()}_MISSING", "warning",
                f"DTC {category} scan was not supplied.",
            ))
        elif not isinstance(values, list):
            findings.append(Finding(
                f"DTC_{category.upper()}_INVALID", "reject",
                f"DTC {category} must be a JSON list.",
            ))
        elif values:
            findings.append(Finding(
                f"DTC_{category.upper()}_PRESENT", "reject",
                f"DTC {category} is not empty: {values}",
            ))

    trims = evidence.get("fuel_trims_percent")
    if not isinstance(trims, dict) or not trims:
        findings.append(Finding(
            "FUEL_TRIMS_MISSING", "warning",
            "Fuel trims for both banks were not supplied.",
        ))
    else:
        for name in ("bank1_ltft", "bank2_ltft", "bank1_stft", "bank2_stft"):
            value = _number(trims.get(name))
            if value is None:
                findings.append(Finding(
                    f"FUEL_TRIM_{name.upper()}_MISSING", "warning",
                    f"Missing numeric fuel trim: {name}",
                ))
            elif abs(value) > trim_reject:
                findings.append(Finding(
                    f"FUEL_TRIM_{name.upper()}_HIGH", "reject",
                    f"{name}={value:.2f}% exceeds ±{trim_reject:.2f}% policy limit.",
                ))
            elif abs(value) > trim_warning:
                findings.append(Finding(
                    f"FUEL_TRIM_{name.upper()}_WARNING", "warning",
                    f"{name}={value:.2f}% exceeds ±{trim_warning:.2f}% warning limit.",
                ))

    minimum_ron = evidence.get("minimum_fuel_ron")
    if minimum_ron not in (91, 95, 98):
        findings.append(Finding(
            "FUEL_RON_MISSING", "warning",
            "minimum_fuel_ron must explicitly be 91, 95, or 98.",
        ))

    severities = {finding.severity for finding in findings}
    evidence_complete = (
        all(name in confirmations for name in REQUIRED_CONFIRMATIONS)
        and all(isinstance(dtcs.get(name), list) for name in ("active", "pending"))
        and isinstance(trims, dict)
        and all(
            _number(trims.get(name)) is not None
            for name in ("bank1_ltft", "bank2_ltft", "bank1_stft", "bank2_stft")
        )
        and minimum_ron in (91, 95, 98)
    )
    if "reject" in severities or not analysis.supported:
        status = "REJECTED"
    elif "warning" in severities:
        status = "NEEDS_DIAGNOSIS"
    else:
        status = "READY"

    binary = asdict(analysis)
    return HealthReport(
        status=status,
        binary=binary,
        stock_provenance=known_name or "unknown",
        evidence_complete=evidence_complete,
        findings=findings,
        policy={"trim_warning_percent": trim_warning, "trim_reject_percent": trim_reject},
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("bin", type=Path, help="24K calibration or 256K full flash")
    parser.add_argument("--evidence", type=Path, help="Stage 0 vehicle evidence JSON")
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    parser.add_argument("--trim-warning", type=float, default=10.0)
    parser.add_argument("--trim-reject", type=float, default=15.0)
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args()

    try:
        evidence = json.loads(args.evidence.read_text(encoding="utf-8")) if args.evidence else None
        if evidence is not None and not isinstance(evidence, dict):
            raise ValueError("evidence root must be a JSON object")
        report = evaluate(
            args.bin, evidence, args.manifest, args.trim_warning, args.trim_reject
        )
    except (OSError, json.JSONDecodeError, ValueError) as exc:
        parser.error(str(exc))

    if args.json:
        print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
    else:
        print(f"{report.status} {args.bin}")
        print(f"  stock provenance: {report.stock_provenance}")
        for finding in report.findings:
            print(f"  [{finding.severity.upper()}] {finding.code}: {finding.message}")
    return {"READY": 0, "NEEDS_DIAGNOSIS": 2, "REJECTED": 1}[report.status]


if __name__ == "__main__":
    raise SystemExit(main())
