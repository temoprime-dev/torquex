#!/usr/bin/env python3
"""Log-derived evidence for decisions that cannot be made without logs.

No real MS41 logs exist yet, so this module is a *placeholder with a contract*:
it defines the file every log-dependent decision will read, validates it, and
makes sure a stub can never be mistaken for data.

Rules:

* A stub (``kind: "stub"``) must have every value null. A stub that carries
  numbers is refused: a placeholder that quietly holds invented values is worse
  than no placeholder at all.
* Real evidence (``kind: "real"``) must name its source, list the channels it was
  built from, and carry non-null values in the sections it claims to cover.
* A section that is missing or null means the corresponding decision stays
  blocked - the tools report REQUIRES_DATA rather than guessing.

Fill the stub once logs exist; nothing else in the pipeline has to change.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:
    from MS41.tools.calibration import DEFAULT_POLICY  # noqa: F401
except ModuleNotFoundError:  # Direct execution from MS41/tools.
    pass

#: Sections a log can answer, with the fields each one needs.
SECTIONS: dict[str, tuple[str, ...]] = {
    "rpm_limit": ("validated", "max_safe_rpm"),
    "knock": ("knock_retard_deg_max", "rpm_at_max_knock"),
    "lambda": ("afr_error_percent_max", "wideband_available"),
    "maf": ("voltage_max_v", "saturated"),
    "trims": ("stft_percent", "ltft_percent"),
    "injectors": ("duty_percent_max", "maf_ceiling_kgh"),
}

STATUS_AVAILABLE = "AVAILABLE"
STATUS_MISSING = "MISSING"
STATUS_STUB = "STUB"


class EvidenceError(ValueError):
    """Raised when an evidence file cannot be trusted."""


@dataclass
class Evidence:
    kind: str
    source: str
    channels: list[str] = field(default_factory=list)
    notes: str = ""
    sections: dict[str, dict[str, Any]] = field(default_factory=dict)

    @property
    def is_stub(self) -> bool:
        return self.kind == "stub"

    def section(self, name: str) -> dict[str, Any] | None:
        """Values of a section, or None unless every field is filled.

        A half-filled section is treated as no data: a lone ``max_safe_rpm``
        without its ``validated`` flag says nothing about the engine.
        """
        values = self.sections.get(name)
        if not values:
            return None
        if any(value is None for value in values.values()):
            return None
        return values

    def status(self, name: str) -> str:
        if self.is_stub:
            return STATUS_STUB
        return STATUS_AVAILABLE if self.section(name) else STATUS_MISSING

    def require(self, name: str, *fields: str) -> dict[str, Any] | None:
        """Return a section only if every required field is present and filled."""
        values = self.section(name)
        if values is None:
            return None
        unknown = [field for field in fields if field not in values]
        if unknown:
            raise EvidenceError(
                f"section {name!r} has no field(s) {unknown}; known fields: "
                f"{list(SECTIONS[name])}"
            )
        return values

    def to_json(self) -> dict[str, Any]:
        return {
            "version": 1,
            "kind": self.kind,
            "source": self.source,
            "channels": self.channels,
            "notes": self.notes,
            "sections": self.sections,
        }

    def summary(self) -> str:
        parts = [f"{name}: {self.status(name)}" for name in SECTIONS]
        return f"{self.kind} evidence from {self.source} — " + ", ".join(parts)


def parse_evidence(data: dict[str, Any]) -> Evidence:
    if not isinstance(data, dict):
        raise EvidenceError("evidence file must be a JSON object")
    kind = str(data.get("kind") or "").strip().lower()
    if kind not in ("stub", "real"):
        raise EvidenceError("EVIDENCE_KIND_MISSING: 'kind' must be 'stub' or 'real'")
    source = str(data.get("source") or "").strip()
    if not source:
        raise EvidenceError("EVIDENCE_SOURCE_MISSING: name the file the evidence came from")
    channels = data.get("channels") or []
    if not isinstance(channels, list):
        raise EvidenceError("'channels' must be a list")
    sections = data.get("sections") or {}
    if not isinstance(sections, dict):
        raise EvidenceError("'sections' must be an object")

    normalised: dict[str, dict[str, Any]] = {}
    for name, fields in SECTIONS.items():
        raw = sections.get(name) or {}
        if not isinstance(raw, dict):
            raise EvidenceError(f"section {name!r} must be an object")
        unknown = set(raw) - set(fields)
        if unknown:
            raise EvidenceError(
                f"section {name!r} has unknown field(s) {sorted(unknown)}; "
                f"known fields: {list(fields)}"
            )
        normalised[name] = {field: raw.get(field) for field in fields}

    if kind == "stub":
        for name, values in normalised.items():
            filled = {field: value for field, value in values.items()
                      if value not in (None, False, "", [])}
            if filled:
                raise EvidenceError(
                    f"STUB_WITH_DATA: the stub claims data in {name!r} ({filled}). A "
                    "placeholder must stay empty; fill the file from real logs first."
                )
    else:
        if not channels:
            raise EvidenceError(
                "EVIDENCE_CHANNELS_MISSING: real evidence must list the channels it "
                "was built from, otherwise its conclusions cannot be checked"
            )
        if not any(normalised[name].get(field) is not None
                   for name, fields in SECTIONS.items() for field in fields):
            raise EvidenceError(
                "EVIDENCE_EMPTY: real evidence with no values in any section; use "
                "'kind': 'stub' while waiting for logs"
            )
    return Evidence(kind=kind, source=source, channels=list(channels),
                    notes=str(data.get("notes") or ""), sections=normalised)


def load_evidence(path: Path) -> Evidence:
    return parse_evidence(json.loads(path.read_text(encoding="utf-8")))


def stub_payload(notes: str = "") -> dict[str, Any]:
    return {
        "version": 1,
        "kind": "stub",
        "source": "no real MS41 logs are available yet",
        "channels": [],
        "notes": notes or (
            "Placeholder. Fill from real logs: run the log through "
            "MS41/tools/analyze_testo_log.py once a real Test0/TestO capture exists, "
            "then replace nulls with measured values and set kind to 'real' and "
            "source to the log file. Stub files must keep every value null."
        ),
        "sections": {
            name: {field: None for field in fields}
            for name, fields in SECTIONS.items()
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Validate the log-evidence contract (a stub until logs exist).")
    parser.add_argument("--check", type=Path, default=None,
                        help="validate an evidence file")
    parser.add_argument("--write-stub", type=Path, default=None,
                        help="write an empty contract file")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    if args.write_stub:
        payload = stub_payload()
        args.write_stub.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
                                   encoding="utf-8")
        print(f"stub written to {args.write_stub}; every value is null and must stay so")
        return 0
    if not args.check:
        parser.error("nothing to do: pass --check FILE or --write-stub FILE")

    try:
        evidence = load_evidence(args.check)
    except (EvidenceError, json.JSONDecodeError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.json:
        print(json.dumps({"evidence": evidence.to_json(),
                          "status": {name: evidence.status(name) for name in SECTIONS}},
                         indent=2, ensure_ascii=False))
    else:
        print(evidence.summary())
        for name, fields in SECTIONS.items():
            values = evidence.section(name)
            state = evidence.status(name)
            detail = (", ".join(f"{field}={values[field]}" for field in fields)
                      if values else "нет данных — решения, зависящие от логов, "
                                     "остаются заблокированными")
            print(f"  [{state}] {name}: {detail}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
