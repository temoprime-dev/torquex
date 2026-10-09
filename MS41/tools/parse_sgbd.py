#!/usr/bin/env python3
"""Parse decoded EDIABAS SGBD (*.prg) documentation for BMW MS41.

EDIABAS ECU description files are distributed as encrypted BEST object files
(*.prg / *.grp). They are decoded into human-readable markdown by tools such as
`radelbro/BimmerDis` and `emdzej/ediabasx`. This parser consumes that decoded
format (jobs, arguments, results and tables) with no third-party dependencies
and turns it into machine-readable diagnostic metadata.

The tool intentionally does not read the encrypted .prg format itself and ships
no BMW binary. Point it at documentation decoded from a licence the user
already has, see `fetch_ms41_sgbd.py`.
"""

from __future__ import annotations

import argparse
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

JOB_HEADING = re.compile(r"^### ([^#\s].*?)\s*$", re.M)
TABLE_HEADING = re.compile(r"^### ([^#\s].*?)\s*$", re.M)
INFO_ROW = re.compile(r"^\|\s*([A-Z_]+)\s*\|\s*(.*?)\s*\|\s*$", re.M)
INDEX_ENTRY = re.compile(r"^- \[([^\]]+)\]\(#(?:job|table)-[^)]*\)(?:\s*-\s*(.*))?$", re.M)
DIMENSIONS = re.compile(r"Dimensions:\s*(\d+)\s*rows?\s*[×x]\s*(\d+)\s*columns?")
SECTION_SPLIT = re.compile(r"^## (Jobs|Tables)\s*$", re.M)


@dataclass
class Field:
    name: str
    type: str
    comment: str


@dataclass
class Job:
    name: str
    slug: str
    description: str
    arguments: list[Field] = field(default_factory=list)
    results: list[Field] = field(default_factory=list)


@dataclass
class Table:
    name: str
    dimensions: tuple[int, int] | None
    columns: list[str]
    rows: list[dict[str, str]]


@dataclass
class Sgbd:
    source: str
    variant: str
    info: dict[str, str]
    jobs: list[Job]
    tables: dict[str, Table]


def _split_sections(text: str) -> dict[str, str]:
    sections: dict[str, str] = {}
    matches = list(SECTION_SPLIT.finditer(text))
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        sections[match.group(1)] = text[match.end():end]
    return sections


def _parse_table(text: str) -> tuple[list[str], list[dict[str, str]]]:
    """Parse the first markdown table found in `text`."""
    lines = [line.strip() for line in text.splitlines()]
    start = next(
        (i for i, line in enumerate(lines) if line.startswith("|") and i + 1 < len(lines)
         and re.fullmatch(r"\|[\s:\-|]+\|", lines[i + 1] or "")),
        None,
    )
    if start is None:
        return [], []
    header = [cell.strip() for cell in lines[start].strip("|").split("|")]
    rows: list[dict[str, str]] = []
    for line in lines[start + 2:]:
        if not line.startswith("|"):
            break
        cells = [cell.strip() for cell in line.strip("|").split("|")]
        if len(cells) != len(header):
            continue
        rows.append(dict(zip(header, cells)))
    return header, rows


def _fields(text: str) -> list[Field]:
    header, rows = _parse_table(text)
    if not header or "Name" not in header:
        return []
    return [
        Field(
            name=row.get("Name", ""),
            type=row.get("Type", ""),
            comment=row.get("Comment", ""),
        )
        for row in rows
        if row.get("Name")
    ]


def _slug(name: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def parse(text: str, source: str = "") -> Sgbd:
    header = re.match(r"^#\s+([A-Za-z0-9_]+)\.(?:prg|grp)\s*$", text, re.M | re.I)
    if header:
        variant = header.group(1)
    elif Path(source).stem:
        variant = Path(source).stem
    else:
        variant = "unknown"

    info = {
        key.strip(): value.strip()
        for key, value in INFO_ROW.findall(text.split("## Jobs")[0])
    }

    sections = _split_sections(text)
    jobs: list[Job] = []
    if "Jobs" in sections:
        body = sections["Jobs"]
        headings = list(JOB_HEADING.finditer(body))
        index_ends = body.find("### Index")
        start = next((match for match in headings if match.start() > index_ends), None)
        for position, match in enumerate(headings):
            if start is None or match.start() < start.start():
                continue
            end = headings[position + 1].start() if position + 1 < len(headings) else len(body)
            chunk = body[match.end():end]
            name = match.group(1).strip()
            description = ""
            for line in chunk.splitlines():
                line = line.strip()
                if line and not line.startswith(("#", "<a id", "|", "_No arguments")):
                    description = line
                    break
            arguments = _fields(chunk.split("#### Arguments")[-1].split("#### Results")[0]) \
                if "#### Arguments" in chunk else []
            results = _fields(chunk.split("#### Results")[-1]) if "#### Results" in chunk else []
            jobs.append(Job(name=name, slug=_slug(name), description=description,
                            arguments=arguments, results=results))

    tables: dict[str, Table] = {}
    if "Tables" in sections:
        body = sections["Tables"]
        headings = list(TABLE_HEADING.finditer(body))
        index_ends = body.find("### Index")
        for position, match in enumerate(headings):
            if index_ends >= 0 and match.start() < index_ends:
                continue
            end = headings[position + 1].start() if position + 1 < len(headings) else len(body)
            chunk = body[match.end():end]
            name = match.group(1).strip()
            if name.lower() == "index":
                continue
            dimensions = DIMENSIONS.search(chunk)
            columns, rows = _parse_table(chunk)
            tables[name] = Table(
                name=name,
                dimensions=(int(dimensions.group(1)), int(dimensions.group(2))) if dimensions else None,
                columns=columns,
                rows=rows,
            )

    return Sgbd(source=source, variant=variant, info=info, jobs=jobs, tables=tables)


def parse_file(path: Path) -> Sgbd:
    return parse(path.read_text(encoding="utf-8", errors="replace"), str(path))


# --------------------------------------------------------------------------
# MS41 semantic mapping: EDIABAS job/result -> canonical logging signal.
# --------------------------------------------------------------------------

# canonical -> (job, result attribute). "WERT" is the numeric value result.
SIGNAL_JOBS: dict[str, tuple[str, str]] = {
    "rpm": ("STATUS_MOTORDREHZAHL", "STATUS_MOTORDREHZAHL_WERT"),
    "coolant_temp": ("STATUS_MOTORTEMPERATUR", "STATUS_MOTORTEMPERATUR_WERT"),
    "intake_temp": ("STATUS_AN_LUFTTEMPERATUR", "STATUS_AN_LUFTTEMPERATUR_WERT"),
    "vehicle_speed": ("STATUS_GESCHWINDIGKEIT", "STATUS_GESCHWINDIGKEIT_WERT"),
    "throttle": ("STATUS_DKP_VOLT", "STATUS_DKP_VOLT_WERT"),
    "battery_voltage": ("STATUS_UBATT", "STATUS_UBATT_WERT"),
    "maf_voltage": ("STATUS_LMM_VOLT", "STATUS_LMM_VOLT_WERT"),
    "maf_airflow": ("STATUS_LMM", "STATUS_LMM_WERT"),
    "load": ("STATUS_LAST", "STATUS_LAST_WERT"),
    "ignition": ("STATUS_ZUENDWINKEL", "STATUS_ZUENDWINKEL_WERT"),
    "injector_pulse_width": ("STATUS_EINSPRITZZEIT", "STATUS_EINSPRITZZEIT_WERT"),
    "lambda_bank1": ("STATUS_L_SONDE", "STATUS_L_SONDE_WERT"),
    "lambda_bank2": ("STATUS_L_SONDE_2", "STATUS_L_SONDE_2_WERT"),
    "knock_bank1": ("STATUS_KLOPF_ADC1", "STATUS_KLOPF_ADC1_WERT"),
    "knock_bank2": ("STATUS_KLOPF_ADC2", "STATUS_KLOPF_ADC2_WERT"),
    "idle_actuator_duty": ("STATUS_TAST_LL_STELLER", "STATUS_TAST_LL_STELLER_WERT"),
    "vanos_angle": ("STATUS_VANOS_VERSTELLWINKEL", "STATUS_VANOS_VERSTELLWINKEL_WERT"),
    "tank_pressure": ("STATUS_TANKDRUCKSENSOR", "STATUS_TANKDRUCKSENSOR_WERT"),
}

# Canonical signal -> RAM cell names used inside BETRIEBSWMATRIX (German).
RAM_NAMES: dict[str, tuple[str, ...]] = {
    "rpm": ("Motordrehzahl",),
    "coolant_temp": ("Motortemperatur",),
    "intake_temp": ("Ansauglufttemp", "Ansauglufttemperatur"),
    "maf_airflow": ("Luftmasse",),
    "load": ("Last",),
    "ignition": ("Zuendwinkel",),
    "injector_pulse_width": ("Einspritzzeit",),
    "battery_voltage": ("Ubatt",),
    "idle_actuator_duty": ("Tast_ll_Steller",),
    "vanos_angle": ("Verstellwinkel",),
    "throttle": ("Dk_winkel",),
    "lambda_bank1": ("Lambdasonde_1",),
    "lambda_bank2": ("Lambdasonde_2",),
    "vehicle_speed": ("Geschwindigkeit", "Fahrgeschw"),
}

# Boolean/status results exposed by the STATUS_DIGITAL job.
STATUS_BITS: tuple[str, ...] = (
    "STATUS_VL_EIN",
    "STATUS_LL_EIN",
    "STATUS_SCHUB_AB_EIN",
    "STATUS_DWA_EIN",
    "STATUS_FS_EIN",
    "STATUS_KO_EIN",
    "STATUS_AC_EIN",
    "STATUS_GE_EIN",
    "STATUS_LAMBDAREGELUNG_1_EIN",
    "STATUS_LAMBDAREGELUNG_2_EIN",
    "STATUS_VANOS_EIN",
    "STATUS_EKP_EIN",
    "STATUS_CAN_EIN",
    "STATUS_ASR_EIN",
    "STATUS_MSR_EIN",
)

BOOLEAN_MEANINGS = {
    "STATUS_VL_EIN": "full-load switch",
    "STATUS_LL_EIN": "idle switch",
    "STATUS_SCHUB_AB_EIN": "overrun fuel cut active",
    "STATUS_LAMBDAREGELUNG_1_EIN": "bank 1 closed loop active",
    "STATUS_LAMBDAREGELUNG_2_EIN": "bank 2 closed loop active",
    "STATUS_VANOS_EIN": "VANOS actuation active",
    "STATUS_KO_EIN": "A/C compressor active",
    "STATUS_MSR_EIN": "MSR/ASC intervention",
}


def to_dict(sgbd: Sgbd) -> dict[str, Any]:
    return asdict(sgbd)


def summary(sgbd: Sgbd) -> dict[str, Any]:
    status_jobs = [job for job in sgbd.jobs if job.name.startswith("STATUS")]
    return {
        "variant": sgbd.variant,
        "source": sgbd.source,
        "ecu": sgbd.info.get("ECU", ""),
        "revision": sgbd.info.get("REVISION", ""),
        "language": sgbd.info.get("SPRACHE", ""),
        "job_count": len(sgbd.jobs),
        "status_job_count": len(status_jobs),
        "tables": {name: list(table.dimensions or ()) for name, table in sgbd.tables.items()},
    }


def channels(sgbd: Sgbd) -> dict[str, Any]:
    """Build the logging channel map for one MS41 variant."""
    by_name = {job.name: job for job in sgbd.jobs}
    result_types: dict[str, str] = {}
    for job in sgbd.jobs:
        for item in job.results:
            result_types.setdefault(item.name, item.type)

    mapped: dict[str, Any] = {}
    for canonical, (job_name, result) in SIGNAL_JOBS.items():
        if job_name in by_name and result in result_types:
            mapped[canonical] = {
                "job": job_name,
                "result": result,
                "type": result_types[result],
                "available": True,
            }
    matrix = sgbd.tables.get("BETRIEBSWMATRIX")
    if matrix:
        for row in matrix.rows:
            cell_name = (row.get("NAME") or "").strip().lower()
            canonical = next(
                (name for name, candidates in RAM_NAMES.items()
                 if any(cell_name.startswith(candidate.lower()) for candidate in candidates)),
                None,
            )
            if canonical and canonical in mapped:
                mapped[canonical]["fast_ram"] = {
                    "name": row.get("NAME"),
                    "source": row.get("QUELLE"),
                    "cell": row.get("ZELLE"),
                    "order": row.get("ORD"),
                    "scale_a": _number(row.get("FAKT_A")),
                    "offset_b": _number(row.get("FAKT_B")),
                    "unit": row.get("EINH"),
                }

    status_digital = by_name.get("STATUS_DIGITAL")
    booleans = []
    if status_digital:
        available = {item.name for item in status_digital.results}
        for name in STATUS_BITS:
            if name in available:
                booleans.append({
                    "result": name,
                    "type": result_types.get(name, "int"),
                    "meaning": BOOLEAN_MEANINGS.get(name, ""),
                })

    return {
        "variant": sgbd.variant,
        "ecu": sgbd.info.get("ECU", ""),
        "signals": mapped,
        "status_digital_job": "STATUS_DIGITAL" if status_digital else None,
        "booleans": booleans,
        "unsupported_signals": [
            name for name in SIGNAL_JOBS if name not in mapped
        ],
    }


def _number(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


# Unit tokens that may legitimately follow "Einheit ..." in a result comment.
UNIT_TOKENS = (
    "kg/h", "mg/Hub", "mg/stroke", "Grad KW", "Grad DK", "Grad C", "Grad",
    "Volt", "ms", "%", "1/min", "hPa", "mbar", "bar", "km/h", "V",
)


def _unit_from_job(job: Job | None, canonical: str) -> tuple[str | None, str | None]:
    if job is None:
        return None, None
    for item in job.results:
        if item.name.endswith("_EINH") and item.comment:
            for token in UNIT_TOKENS:
                if token in item.comment:
                    return token, "einh_comment"
        if item.name.endswith("_WERT") and item.comment:
            for token in UNIT_TOKENS:
                if token in item.comment:
                    return token, "wert_comment"
    return None, None


# Recommended Test0/TestO logging profiles, expressed in canonical signal names.
LOGGING_PRESETS: dict[str, tuple[str, ...]] = {
    "stage0": (
        "rpm", "coolant_temp", "intake_temp", "maf_airflow", "ignition",
        "battery_voltage", "vehicle_speed", "lambda_bank1", "lambda_bank2",
    ),
    "wot": (
        "rpm", "throttle", "load", "maf_airflow", "maf_voltage", "ignition",
        "injector_pulse_width", "coolant_temp", "intake_temp", "vehicle_speed",
        "knock_bank1", "knock_bank2", "lambda_bank1", "lambda_bank2",
    ),
    "ignition": (
        "rpm", "load", "ignition", "intake_temp", "coolant_temp",
        "knock_bank1", "knock_bank2",
    ),
    "fueling": (
        "rpm", "load", "maf_airflow", "maf_voltage", "injector_pulse_width",
        "lambda_bank1", "lambda_bank2", "coolant_temp", "intake_temp", "throttle",
    ),
}


def alias_map(sgbd: Sgbd) -> dict[str, str]:
    """Exact EDIABAS result name -> canonical logging signal."""
    mapping: dict[str, str] = {}
    available = {job.name for job in sgbd.jobs}
    for canonical, (job, result) in SIGNAL_JOBS.items():
        mapping[result.upper()] = canonical
        mapping[canonical.upper()] = canonical
        if job in available:
            mapping.setdefault(job.upper(), canonical)
    digital = next((job for job in sgbd.jobs if job.name == "STATUS_DIGITAL"), None)
    if digital:
        for item in digital.results:
            if item.name in BOOLEAN_MEANINGS:
                mapping[item.name.upper()] = item.name
    return mapping


def presets(sgbd: Sgbd) -> dict[str, Any]:
    """Concrete Test0 setup: which jobs to poll and which results to log."""
    channel_map = channels(sgbd)["signals"]
    by_name = {job.name: job for job in sgbd.jobs}
    result_types: dict[str, str] = {}
    for job in sgbd.jobs:
        for item in job.results:
            result_types.setdefault(item.name, item.type)

    output: dict[str, Any] = {"variant": sgbd.variant, "ecu": sgbd.info.get("ECU", ""),
                              "profiles": {}}
    for profile, signals in LOGGING_PRESETS.items():
        entries = []
        for canonical in signals:
            mapped = channel_map.get(canonical)
            if not mapped:
                continue
            job = by_name.get(mapped["job"])
            unit, unit_source = _unit_from_job(job, canonical)
            fast = mapped.get("fast_ram") or {}
            entries.append({
                "signal": canonical,
                "job": mapped["job"],
                "result": mapped["result"],
                "type": mapped["type"],
                "unit": fast.get("unit") or unit,
                "unit_source": "betriebswmatrix" if fast.get("unit") else unit_source,
            })
        output["profiles"][profile] = {
            "jobs": sorted({entry["job"] for entry in entries}),
            "results": entries,
            "missing": [name for name in signals if name not in channel_map],
        }
    return output


def write_bundle(sgbds: list[Sgbd], outdir: Path) -> list[Path]:
    outdir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []

    def dump(name: str, payload: Any) -> None:
        path = outdir / name
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )
        written.append(path)

    dump("ms41_sgbd_index.json", [summary(item) for item in sgbds])
    dump("ms41_channels.json", [channels(item) for item in sgbds])
    dump("ms41_faults.json", {item.variant: faults(item) for item in sgbds})
    dump("ms41_jobs.json", {
        item.variant: [
            {
                "name": job.name,
                "description": job.description,
                "arguments": [asdict(argument) for argument in job.arguments],
                "results": [asdict(result) for result in job.results],
            }
            for job in item.jobs
        ]
        for item in sgbds
    })
    dump("ms41_logging_presets.json", [presets(item) for item in sgbds])
    dump("ms41_aliases.json", {
        item.variant: alias_map(item) for item in sgbds
    })
    return written


def faults(sgbd: Sgbd) -> list[dict[str, str]]:
    table = sgbd.tables.get("FORTTEXTE")
    if not table:
        return []
    entries = []
    for row in table.rows:
        code = row.get("ORT", "")
        text = row.get("ORTTEXT", "")
        if code and text not in {"", "--"}:
            entries.append({"code": code, "text": text})
    return entries


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sgbd", nargs="+", type=Path, help="decoded SGBD markdown file(s)")
    parser.add_argument(
        "--emit",
        choices=("summary", "channels", "faults", "jobs", "presets", "json"),
        default="summary",
    )
    parser.add_argument("--outdir", type=Path,
                        help="write the full diagnostics bundle into this directory")
    parser.add_argument("--pretty", action="store_true")
    args = parser.parse_args()

    parsed = [parse_file(path) for path in args.sgbd]
    if args.outdir:
        for path in write_bundle(parsed, args.outdir):
            print(path)
        return 0
    if args.emit == "summary":
        payload: Any = [summary(item) for item in parsed]
    elif args.emit == "channels":
        payload = [channels(item) for item in parsed]
    elif args.emit == "faults":
        payload = {item.variant: faults(item) for item in parsed}
    elif args.emit == "jobs":
        payload = {
            item.variant: [
                {"name": job.name, "description": job.description,
                 "results": [result.name for result in job.results]}
                for job in item.jobs
            ]
            for item in parsed
        }
    elif args.emit == "presets":
        payload = [presets(item) for item in parsed]
    else:
        payload = [to_dict(item) for item in parsed]
    print(json.dumps(payload, ensure_ascii=False, indent=2 if args.pretty else None))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
