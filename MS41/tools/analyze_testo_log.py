#!/usr/bin/env python3
"""Read Test0/TestO CSV logs and assess their suitability for MS41 tuning.

Supports both legacy TestO exports and Test0 v2 CSV with flexible delimiters,
encodings, timestamps and EDIABAS result names. The tool does not guess units
or treat missing channels as safe values.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import statistics
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

PROFILES = {
    "stage0": ("rpm", "coolant_temp", "intake_temp", "maf_airflow", "ignition"),
    "wot": (
        "rpm", "throttle", "load", "maf_airflow", "maf_voltage", "ignition",
        "injector_pulse_width", "lambda_wideband",
    ),
    "ignition": ("rpm", "load", "ignition", "intake_temp", "coolant_temp", "knock"),
    "fueling": (
        "rpm", "load", "maf_airflow", "maf_voltage", "injector_pulse_width",
        "lambda_wideband",
    ),
}

# A required profile signal is satisfied by any of these canonical names.
SATISFIED_BY: dict[str, tuple[str, ...]] = {
    "knock": ("knock", "knock_bank1", "knock_bank2"),
    "lambda_wideband": ("lambda_wideband",),
}

# Exact EDIABAS job/result names for MS41, generated from the decoded SGBD files
# by `parse_sgbd.py`. Optional: the parser degrades to fuzzy matching without it.
DEFAULT_ALIASES = Path(__file__).parents[1] / "diagnostics" / "ms41_aliases.json"
_ALIAS_CACHE: dict[str, str] | None = None

# Patterns intentionally cover native EDIABAS German names and common English
# labels. More specific signals are matched before broader ones.
ALIASES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("time", ("time", "timestamp", "zeit", "uhrzeit", "seconds")),
    ("lambda_wideband", ("wideband", "wbo2", "lambda_ext", "lambda external", "afr")),
    ("maf_voltage", ("lmm_spann", "maf voltage", "hfm voltage", "v_maf")),
    ("maf_airflow", ("status_lmm_wert", "stat_lmm", "luftmasse", "air mass", "airflow", "maf")),
    ("injector_pulse_width", ("einspritzzeit", "injection time", "injector pulse", "pulsewidth", "ipw")),
    ("coolant_temp", ("kuehlmittel", "kuhlmittel", "coolant", "tco", "motortemp")),
    ("intake_temp", ("ansaugluft", "intake air", "iat", "tia")),
    ("throttle", ("drosselklappe", "pedalwert", "throttle", "pvs", "tps")),
    ("rpm", ("motordrehzahl", "engine speed", "drehzahl", "rpm")),
    ("vehicle_speed", ("fahrgeschwindigkeit", "vehicle speed", "geschwindigkeit", "speed", "km/h")),
    ("ignition", ("zuendwinkel", "zundwinkel", "ignition angle", "ignition timing", "spark advance")),
    ("load", ("status_last", "stat_last", "engine load", "motorlast", "load", "mg/stroke")),
    ("fuel_trim", ("adaptation", "fuel trim", "lambda integrator", "gemischadapt")),
    ("knock", ("klopf", "knock", "retard")),
    ("battery_voltage", ("batteriespann", "battery voltage", "ubatt")),
)


@dataclass(frozen=True)
class Channel:
    source: str
    canonical: str | None
    unit: str | None
    samples: int
    missing: int
    minimum: float | None
    maximum: float | None
    mean: float | None
    changes: int


@dataclass(frozen=True)
class LogReport:
    status: str
    path: str
    format: str
    encoding: str
    delimiter: str
    rows: int
    duration_seconds: float | None
    sample_rate_hz: float | None
    profile: str
    missing_required: list[str]
    warnings: list[str]
    channels: list[Channel]


def _normalise(text: str) -> str:
    text = text.strip().lower().replace("ä", "a").replace("ö", "o").replace("ü", "u")
    return re.sub(r"[^a-z0-9%/]+", " ", text)


def ediabas_aliases() -> dict[str, str]:
    """Exact MS41 result/job names -> canonical signals, merged across variants."""
    global _ALIAS_CACHE
    if _ALIAS_CACHE is None:
        merged: dict[str, str] = {}
        try:
            data = json.loads(DEFAULT_ALIASES.read_text(encoding="utf-8"))
            for variant_map in data.values():
                for result, canonical in variant_map.items():
                    merged.setdefault(result.upper(), canonical)
        except (OSError, json.JSONDecodeError, AttributeError):
            merged = {}
        _ALIAS_CACHE = merged
    return _ALIAS_CACHE


def canonical_name(name: str) -> str | None:
    head = re.split(r"[\[(;,\t]", name.strip())[0].strip()
    exact = ediabas_aliases().get(head.upper())
    if exact:
        return exact
    normal = _normalise(name)
    compact = normal.replace(" ", "_")
    for canonical, aliases in ALIASES:
        if any(_normalise(alias) in normal or _normalise(alias).replace(" ", "_") in compact for alias in aliases):
            return canonical
    return None


def _unit(name: str) -> str | None:
    match = re.search(r"[\[(]([^\]\)]+)[\]\)]", name)
    return match.group(1).strip() if match else None


def _float(value: str, decimal_comma: bool) -> float | None:
    value = value.strip().strip('"')
    if not value or value.lower() in {"nan", "n/a", "null", "-"}:
        return None
    if decimal_comma:
        value = value.replace(".", "").replace(",", ".")
    try:
        number = float(value)
        return number if math.isfinite(number) else None
    except ValueError:
        return None


#: Naive timestamps are converted against a fixed base, not the local zone.
_EPOCH = datetime(1970, 1, 1)


def _time(value: str, decimal_comma: bool) -> float | None:
    numeric = _float(value, decimal_comma)
    if numeric is not None:
        return numeric
    value = value.strip()
    for pattern in (
        "%H:%M:%S.%f", "%H:%M:%S,%f", "%H:%M:%S",
        "%d.%m.%Y %H:%M:%S.%f", "%d.%m.%Y %H:%M:%S",
        "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d %H:%M:%S",
    ):
        try:
            moment = datetime.strptime(value, pattern)
        except ValueError:
            continue
        # ``datetime.timestamp()`` reads a naive datetime as local time and on
        # Windows raises OSError for dates the C library cannot represent, so a
        # plain epoch difference is used instead: it is timezone-free and works
        # for every date. Only differences between rows are ever reported
        # (duration, sample rate), so the choice of epoch base does not matter.
        return (moment - _EPOCH).total_seconds()
    return None


def _detect_delimiter(text: str) -> str:
    """Pick the delimiter that gives the most consistent column count.

    TestO/Test0 exports mix English and German conventions, and thousands or
    decimal separators can confuse a generic sniffer. Consistency across the
    header and the first rows is a more reliable signal.
    """
    lines = [line for line in text.splitlines()[:200] if line.strip()]
    sample = lines[:40]
    best_delimiter, best_score = ",", (False, 0.0, 0)
    for candidate in (";", ",", "\t", "|"):
        counts = [len(next(csv.reader([line], delimiter=candidate))) for line in sample]
        if not counts or max(counts) < 2:
            continue
        mode = max(set(counts), key=lambda value: (counts.count(value), value))
        consistency = counts.count(mode) / len(counts)
        # A real delimiter must split the header line; consistency breaks ties,
        # and only then the widest reading wins.
        score = (counts[0] >= 2, round(consistency, 4), mode)
        if score > best_score:
            best_delimiter, best_score = candidate, score
    if not best_score[0] and best_score[2] < 2:
        best_delimiter = ";" if text.count(";") >= text.count(",") else ","
    return best_delimiter


def _read(path: Path) -> tuple[list[str], list[list[str]], str, str]:
    raw = path.read_bytes()
    encoding = "utf-8-sig"
    try:
        text = raw.decode(encoding)
    except UnicodeDecodeError:
        encoding = "cp1252"
        text = raw.decode(encoding)
    if not text.strip():
        raise ValueError("log is empty")

    delimiter = _detect_delimiter(text)
    rows = [row for row in csv.reader(text.splitlines(), delimiter=delimiter) if any(cell.strip() for cell in row)]
    if len(rows) < 2:
        raise ValueError("log must contain a header and at least one data row")
    width = max(map(len, rows))
    header_index = next(
        (i for i, row in enumerate(rows[:10]) if len(row) >= 2 and sum(bool(canonical_name(x)) for x in row) >= 1),
        0,
    )
    header = rows[header_index] + [f"unnamed_{i}" for i in range(len(rows[header_index]), width)]
    data = [(row + [""] * width)[:width] for row in rows[header_index + 1:]]
    return header, data, encoding, delimiter


def analyze_log(path: Path, profile: str = "wot") -> LogReport:
    if profile not in PROFILES:
        raise ValueError(f"unknown profile {profile!r}")
    header, rows, encoding, delimiter = _read(path)
    decimal_comma = delimiter != "," and any(re.search(r"\d,\d", cell) for row in rows[:20] for cell in row)

    canonicals = [canonical_name(name) for name in header]
    time_index = next((i for i, name in enumerate(canonicals) if name == "time"), None)
    times = [_time(row[time_index], decimal_comma) for row in rows] if time_index is not None else []
    valid_times = [value for value in times if value is not None]
    duration = valid_times[-1] - valid_times[0] if len(valid_times) >= 2 else None
    rate = (len(valid_times) - 1) / duration if duration and duration > 0 else None

    channels: list[Channel] = []
    present: set[str] = set()
    warnings: list[str] = []
    duplicate: dict[str, int] = {}
    for index, source in enumerate(header):
        canonical = canonicals[index]
        values = [_float(row[index], decimal_comma) for row in rows]
        numeric = [value for value in values if value is not None]
        if canonical:
            present.add(canonical)
            duplicate[canonical] = duplicate.get(canonical, 0) + 1
        changes = sum(a != b for a, b in zip(numeric, numeric[1:]))
        channels.append(Channel(
            source=source,
            canonical=canonical,
            unit=_unit(source),
            samples=len(numeric),
            missing=len(rows) - len(numeric),
            minimum=min(numeric) if numeric else None,
            maximum=max(numeric) if numeric else None,
            mean=statistics.fmean(numeric) if numeric else None,
            changes=changes,
        ))

    missing = [
        name for name in PROFILES[profile]
        if not any(candidate in present for candidate in SATISFIED_BY.get(name, (name,)))
    ]
    if "lambda_wideband" in missing and {"lambda_bank1", "lambda_bank2"} & present:
        warnings.append(
            "Only narrowband MS41 oxygen sensors were found. They confirm closed-loop "
            "behaviour but cannot validate wide-open-throttle fueling."
        )
    if time_index is None:
        warnings.append("Timestamp channel not recognised; duration and sample rate are unavailable.")
    elif len(valid_times) != len(rows):
        warnings.append(f"Timestamp is missing or invalid in {len(rows) - len(valid_times)} rows.")
    if rate is not None and rate < 5:
        warnings.append(f"Average row rate is only {rate:.2f} Hz; transient/WOT interpretation may be unreliable.")
    if duration is not None and duration < 5:
        warnings.append(f"Log duration is only {duration:.2f} seconds.")
    for name, count in duplicate.items():
        if name != "knock" and count > 1:
            warnings.append(f"Multiple columns map to {name}; inspect source names and units.")
    unknown = sum(channel.canonical is None for channel in channels)
    if unknown:
        warnings.append(f"{unknown} channel(s) are not mapped; original columns remain in the report.")

    if not rows:
        status = "INVALID"
    elif missing:
        status = "INCOMPLETE"
    else:
        status = "READY_FOR_ANALYSIS"
    return LogReport(
        status=status,
        path=str(path),
        format="Test0/TestO CSV",
        encoding=encoding,
        delimiter={"\t": "TAB"}.get(delimiter, delimiter),
        rows=len(rows),
        duration_seconds=duration,
        sample_rate_hz=rate,
        profile=profile,
        missing_required=missing,
        warnings=warnings,
        channels=channels,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("log", type=Path)
    parser.add_argument("--profile", choices=sorted(PROFILES), default="wot")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    try:
        report = analyze_log(args.log, args.profile)
    except (OSError, ValueError) as exc:
        parser.error(str(exc))
    if args.json:
        print(json.dumps(asdict(report), ensure_ascii=False, indent=2))
    else:
        print(f"{report.status} {args.log}: {report.rows} rows, profile={report.profile}")
        print(f"  duration={report.duration_seconds} s rate={report.sample_rate_hz} Hz")
        if report.missing_required:
            print("  missing required: " + ", ".join(report.missing_required))
        for warning in report.warnings:
            print(f"  [WARNING] {warning}")
        for channel in report.channels:
            print(
                f"  {channel.source} -> {channel.canonical or 'unmapped'}: "
                f"n={channel.samples} min={channel.minimum} max={channel.maximum}"
            )
    return 0 if report.status == "READY_FOR_ANALYSIS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
