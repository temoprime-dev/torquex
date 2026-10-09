# -*- coding: utf-8 -*-
"""Shared Stage helpers for the MS42/MS43 generators.

This module contains policy-free mathematics and the public release gates.  ECU
addresses, XDF objects, scales and checksum handling stay in the version-specific
cores.  The helpers do not promise power: they create bounded requests which are
checked once more by the version policy and calibration core before bytes are
written.
"""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any, Iterable

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_CONFIG = REPO_ROOT / "config" / "torquex.json"
DEFAULT_PEDAL_CONFIG = REPO_ROOT / "config" / "pedal_response.json"
DEFAULT_BENCH_REGISTRY = REPO_ROOT / "config" / "bench_verified.json"

#: Pedal response and warm VANOS remain behind the release gate.  Ignition has
#: a public conservative Stage 1 path; only its linked/extended path is gated.
BENCH_GATED_KINDS = frozenset({"pedal_response", "vanos_stage1"})
PUBLIC_IGNITION_KINDS = frozenset({"ignition", "ignition_stage1"})
BENCH_FEATURE_KINDS = BENCH_GATED_KINDS | PUBLIC_IGNITION_KINDS
SUPPORTED_PEDAL_LEVELS = (1, 2, 3)


def _env_bool(name: str) -> bool | None:
    raw = os.environ.get(name)
    if raw is None:
        return None
    value = raw.strip().lower()
    if value in {"1", "true", "yes", "on", "да"}:
        return True
    if value in {"0", "false", "no", "off", "нет"}:
        return False
    raise ValueError(f"{name} must be true/false, got {raw!r}")


def _json_file(path: str | Path, fallback: dict[str, Any]) -> dict[str, Any]:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, OSError, json.JSONDecodeError):
        return fallback
    return value if isinstance(value, dict) else fallback


def bench_required(config_path: str | Path | None = None) -> bool:
    """Return the release gate, defaulting safely to ``true``."""
    override = _env_bool("TORQUEX_BENCH_REQUIRED")
    if override is not None:
        return override
    path = Path(config_path or os.environ.get("TORQUEX_CONFIG_PATH") or DEFAULT_CONFIG)
    payload = _json_file(path, {"bench_required": True})
    return bool(payload.get("bench_required", True))


def _registry_records(path: str | Path | None = None) -> list[dict[str, Any]]:
    """Read only explicit, auditable records from the tracked bench registry.

    ``verified`` is the canonical key.  ``versions`` is accepted for a smooth
    migration of deployment-local registries, but no environment flag can make
    an entry appear here.  A record needs a platform, exact software version,
    date and a field-report reference before it can grant trust.
    """
    registry = Path(path or os.environ.get("TORQUEX_BENCH_VERIFIED_PATH")
                    or DEFAULT_BENCH_REGISTRY)
    payload = _json_file(registry, {})
    records = payload.get("verified", payload.get("versions", []))
    if isinstance(records, dict):
        records = list(records.values())
    if not isinstance(records, list):
        return []
    return [record for record in records if isinstance(record, dict)]


def bench_verified(platform: str, version: str | None,
                   *, registry_path: str | Path | None = None) -> bool:
    """Whether this exact platform/version has an auditable bench record."""
    if not version:
        return False
    wanted_platform = str(platform).upper()
    wanted_version = str(version).upper()
    for record in _registry_records(registry_path):
        record_platform = str(record.get("platform", record.get("ecu", ""))).upper()
        record_version = str(record.get("version", record.get("software", ""))).upper()
        report = (record.get("field_report") or record.get("field_report_url")
                  or record.get("field_report_path") or record.get("report")
                  or record.get("report_url") or record.get("report_path")
                  or record.get("link"))
        verified_date = record.get("verified_at") or record.get("date")
        if (record_platform == wanted_platform and record_version == wanted_version
                and str(verified_date or "").strip() and str(report).strip()):
            return True
    return False


def release_advanced_allowed(platform: str, version: str | None) -> bool:
    """Whether the API may expose gated/linked MS42/MS43 packages."""
    return (not bench_required()) or bench_verified(platform, version)


def advanced_intents(intents: Iterable[dict[str, Any]], platform: str) -> list[dict[str, Any]]:
    """Select all MS42/MS43 round-5b intents for release annotations."""
    if str(platform).upper() not in {"MS42", "MS43"}:
        return []
    return [
        item for item in intents
        if isinstance(item, dict) and str(item.get("kind", "")) in BENCH_FEATURE_KINDS
    ]


def protected_intents(intents: Iterable[dict[str, Any]], platform: str) -> list[dict[str, Any]]:
    """Select only packages which must be rejected while the bench gate is on.

    Ignition deliberately does not appear here: the server injects a
    conservative RON98-only, unlinked Stage 1 request while the gate is on.
    """
    return [item for item in advanced_intents(intents, platform)
            if str(item.get("kind", "")) in BENCH_GATED_KINDS]


def bench_gate_finding(platform: str, intents: Iterable[dict[str, Any]], *,
                       version: str | None = None,
                       config_path: str | Path | None = None,
                       registry_path: str | Path | None = None) -> dict[str, str] | None:
    """Return the stable API finding when a gated feature is not bench-cleared."""
    requested = protected_intents(intents, platform)
    required = bench_required(config_path)
    verified = bench_verified(platform, version, registry_path=registry_path)
    if not requested or not required or verified:
        return None
    kinds = ", ".join(sorted({str(item.get("kind")) for item in requested}))
    return {
        "code": "AWAITING_BENCH_VERIFICATION",
        "severity": "reject",
        "message": (
            f"{platform}: {kinds} собран и проверен байтовыми тестами, но скрыт "
            "до стендовой проверки; API не выпускает файл при bench_required=true "
            "без записи в config/bench_verified.json"
        ),
    }


def _pedal_config(config_path: str | Path | None = None) -> dict[str, Any]:
    fallback = {
        "levels": {
            "1": {"surface_percent": 0.08, "threshold_percent": 0.10},
            "2": {"surface_percent": 0.15, "threshold_percent": 0.20},
            "3": {"surface_percent": 0.24, "threshold_percent": 0.30},
        },
        "transmissions": {"mt": {"surface_multiplier": 1.0},
                          "at": {"surface_multiplier": 0.65}},
        "surface": {"row_floor": 0.85, "row_span": 0.15,
                    "max_neighbour_stock_delta_factor": 2.0},
    }
    return _json_file(config_path or os.environ.get("TORQUEX_PEDAL_CONFIG_PATH")
                      or DEFAULT_PEDAL_CONFIG, fallback)


def _gearbox_name(transmission: str | None) -> str:
    gearbox = str(transmission or "at").lower()
    if gearbox in {"automatic", "auto", "at"}:
        return "at"
    if gearbox in {"manual", "mt"}:
        return "mt"
    raise ValueError("transmission must be MT/manual or AT/automatic")


def pedal_risk_acknowledged(intent: dict[str, Any], level: int) -> tuple[bool, str | None]:
    """Validate base risk and the separate level-2/3 acknowledgement."""
    if not (intent.get("acknowledge_risk") or intent.get("acknowledge_pedal_risk")):
        return False, "ACKNOWLEDGE_RISK_REQUIRED"
    if level >= 2 and not any(intent.get(key) for key in (
        "acknowledge_level_risk", "acknowledge_pedal_level_risk",
        "acknowledge_risk_level", "acknowledge_pedal_level",
    )):
        return False, "ACKNOWLEDGE_LEVEL_RISK_REQUIRED"
    return True, None


def _normalised(index: int, count: int) -> float:
    return 0.0 if count <= 1 else index / float(count - 1)


def _source_monotonic(values: list[float]) -> str | None:
    increasing = all(right + 1e-12 >= left for left, right in zip(values, values[1:]))
    decreasing = all(left + 1e-12 >= right for left, right in zip(values, values[1:]))
    if increasing:
        return "increasing"
    if decreasing:
        return "decreasing"
    return None


def _project_monotonic(target: list[float], stock: list[float], rows: int,
                       columns: int) -> None:
    """Project a target surface without ever falling below its stock surface."""
    for row in range(rows):
        start = row * columns
        kind = _source_monotonic(stock[start:start + columns])
        if kind == "increasing":
            for col in range(columns - 2, -1, -1):
                target[start + col] = min(target[start + col], target[start + col + 1])
        elif kind == "decreasing":
            for col in range(1, columns):
                target[start + col] = min(target[start + col], target[start + col - 1])
    for col in range(columns):
        source = [stock[row * columns + col] for row in range(rows)]
        kind = _source_monotonic(source)
        if kind == "increasing":
            for row in range(rows - 2, -1, -1):
                index = row * columns + col
                target[index] = min(target[index], target[index + columns])
        elif kind == "decreasing":
            for row in range(1, rows):
                index = row * columns + col
                target[index] = min(target[index], target[index - columns])


def _smooth_delta_neighbours(target: list[float], stock: list[float], rows: int,
                             columns: int, factor: float) -> None:
    """Only reduce an excessive new jump; protected pedal edges stay stock."""
    if factor < 1.0:
        raise ValueError("max_neighbour_stock_delta_factor must be at least 1")
    protected = {row * columns for row in range(rows)}
    protected.update({row * columns + columns - 1 for row in range(rows)})

    def pair(left: int, right: int) -> None:
        limit = factor * abs(stock[left] - stock[right]) + 1e-12
        difference = target[left] - target[right]
        if difference > limit and left not in protected:
            target[left] = max(stock[left], target[right] + limit)
        elif -difference > limit and right not in protected:
            target[right] = max(stock[right], target[left] - limit)

    for _ in range(max(2, rows + columns)):
        before = tuple(target)
        for row in range(rows):
            for col in range(columns - 1):
                pair(row * columns + col, row * columns + col + 1)
        for row in range(rows - 1):
            for col in range(columns):
                pair(row * columns + col, (row + 1) * columns + col)
        if tuple(target) == before:
            break


def pedal_response_delta_model(stock: list[float], rows: int, columns: int,
                               level: int, transmission: str | None = None,
                               maximum: float | None = None,
                               config_path: str | Path | None = None) -> list[float]:
    """Build a non-negative pedal-response delta from the measured stock map.

    The endpoint columns and zero stock cells are immutable.  Every interior
    delta is a configured percentage of its own stock cell, shaped by pedal and
    RPM; no replacement map or value copied from another tune is involved.
    """
    if len(stock) != rows * columns:
        raise ValueError("torque surface shape does not match stock values")
    if level not in SUPPORTED_PEDAL_LEVELS:
        raise ValueError(f"pedal level must be one of {SUPPORTED_PEDAL_LEVELS}")
    gearbox = _gearbox_name(transmission)
    config = _pedal_config(config_path)
    level_config = config.get("levels", {}).get(str(level), {})
    percent = float(level_config.get("surface_percent", 0.0))
    gearbox_config = config.get("transmissions", {}).get(gearbox, {})
    percent *= float(gearbox_config.get("surface_multiplier", 1.0))
    surface = config.get("surface", {})
    row_floor = float(surface.get("row_floor", 0.85))
    row_span = float(surface.get("row_span", 0.15))
    if percent < 0 or row_floor < 0 or row_span < 0 or row_floor + row_span > 1.0 + 1e-12:
        raise ValueError("pedal response config has an invalid non-negative envelope")

    target: list[float] = []
    for row in range(rows):
        rpm_shape = row_floor + row_span * _normalised(row, rows)
        for column in range(columns):
            current = max(0.0, float(stock[row * columns + column]))
            pedal = _normalised(column, columns)
            pedal_shape = math.sin(math.pi * pedal) if 0.0 < pedal < 1.0 else 0.0
            target.append(current + current * percent * pedal_shape * rpm_shape)

    _project_monotonic(target, [float(value) for value in stock], rows, columns)
    factor = float(surface.get("max_neighbour_stock_delta_factor", 2.0))
    _smooth_delta_neighbours(target, [float(value) for value in stock], rows, columns, factor)
    # Re-assert the two hard invariants after smoothing and policy cap.
    for index, current in enumerate(stock):
        current = float(current)
        target[index] = max(current, target[index])
        if maximum is not None:
            if float(maximum) < current:
                raise ValueError("policy maximum is below a stock cell")
            target[index] = min(float(maximum), target[index])
    for row in range(rows):
        target[row * columns] = float(stock[row * columns])
        target[row * columns + columns - 1] = float(stock[row * columns + columns - 1])
    return target


# Public descriptive alias used by reports and future callers.
pedal_delta_model = pedal_response_delta_model


def monotone_torque_model(stock: list[float], rows: int, columns: int,
                          level: int, transmission: str | None = None,
                          maximum: float | None = None) -> list[float]:
    """Backward-compatible name for the stock-plus-delta surface builder."""
    return pedal_response_delta_model(stock, rows, columns, level, transmission, maximum)


def pedal_threshold_model(stock: list[float], level: int,
                          transmission: str | None = None,
                          minimum: float | None = None,
                          maximum: float | None = None,
                          config_path: str | Path | None = None) -> list[float]:
    """Shift full-load pedal thresholds down by configured 10/20/30 percent."""
    if level not in SUPPORTED_PEDAL_LEVELS:
        raise ValueError(f"pedal level must be one of {SUPPORTED_PEDAL_LEVELS}")
    _gearbox_name(transmission)  # validate MT/AT; the threshold percentage is common
    config = _pedal_config(config_path)
    amount = float(config.get("levels", {}).get(str(level), {}).get("threshold_percent", 0.0))
    if not 0.0 <= amount <= 1.0:
        raise ValueError("threshold_percent must be between 0 and 1")
    out: list[float] = []
    for value in stock:
        result = max(0.0, float(value) * (1.0 - amount))
        if minimum is not None:
            result = max(float(minimum), result)
        if maximum is not None:
            result = min(float(maximum), result)
        out.append(result)
    return out


def monotonic_increasing(values: Iterable[float]) -> bool:
    values = list(values)
    return all(right + 1e-12 >= left for left, right in zip(values, values[1:]))


def cautious_vanos_shift(intent: dict[str, Any], intake_default: float = -1.5,
                         exhaust_default: float = 1.5) -> tuple[float, float]:
    """Read the warm-map Stage 1 shifts and enforce the requested ±5° envelope."""
    try:
        intake = float(intent.get("shift_in_deg", intake_default))
        exhaust = float(intent.get("shift_ex_deg", exhaust_default))
    except (TypeError, ValueError) as error:
        raise ValueError("VANOS shifts must be numeric") from error
    if abs(intake) > 5.0 or abs(exhaust) > 5.0:
        raise ValueError("VANOS Stage 1 shifts must stay within stock ±5°")
    return intake, exhaust


def public_ignition_caps(limit: float, rpm_axis: list[float], columns: int,
                          config_path: str | Path | None = None) -> list[float]:
    """Return per-row ceilings for the public no-linked-MBT ignition profile."""
    config = _json_file(config_path or os.environ.get("TORQUEX_CONFIG_PATH")
                        or DEFAULT_CONFIG, {})
    settings = config.get("ignition_public", {})
    threshold = float(settings.get("rpm_threshold", 3000.0))
    high_rpm_factor = float(settings.get("high_rpm_factor", 0.50))
    if not 0.0 < high_rpm_factor <= 1.0:
        raise ValueError("ignition_public.high_rpm_factor must be in (0, 1]")
    return [
        min(float(limit), float(limit) * (1.0 if float(rpm) <= threshold
                                           else high_rpm_factor))
        for rpm in rpm_axis for _ in range(columns)
    ]


def trust_level(*, bench_verified: bool) -> str:
    return "bench verified" if bench_verified else "bytes+crc verified"
