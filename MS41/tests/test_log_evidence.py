#!/usr/bin/env python3
"""Tests for the log-evidence contract (a documented stub until logs exist)."""

from __future__ import annotations

import copy
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

from MS41.tools import log_evidence  # noqa: E402
from MS41.tools.generate_patches import generate  # noqa: E402
from MS41.tools.log_evidence import (  # noqa: E402
    STATUS_AVAILABLE,
    STATUS_MISSING,
    STATUS_STUB,
    EvidenceError,
    load_evidence,
    parse_evidence,
    stub_payload,
)

EXAMPLES = ROOT / "MS41" / "examples"
STUB_FILE = EXAMPLES / "log_evidence.stub.json"
TOOL = ROOT / "MS41" / "tools" / "log_evidence.py"


def real_payload(**sections) -> dict:
    payload = stub_payload()
    payload["kind"] = "real"
    payload["source"] = "test0_2026-05-01.csv"
    payload["channels"] = ["n", "maf", "tps"]
    for name, values in sections.items():
        payload["sections"][name].update(values)
    return payload


class ContractTests(unittest.TestCase):
    def test_stub_has_every_value_null(self):
        evidence = parse_evidence(stub_payload())
        self.assertTrue(evidence.is_stub)
        for name in log_evidence.SECTIONS:
            self.assertIsNone(evidence.section(name), name)
            self.assertEqual(evidence.status(name), STATUS_STUB)

    def test_shipped_stub_file_validates(self):
        evidence = load_evidence(STUB_FILE)
        self.assertTrue(evidence.is_stub)
        self.assertTrue(evidence.source)

    def test_stub_with_data_is_refused(self):
        payload = stub_payload()
        payload["sections"]["rpm_limit"]["max_safe_rpm"] = 6800
        with self.assertRaises(EvidenceError) as caught:
            parse_evidence(payload)
        self.assertIn("STUB_WITH_DATA", str(caught.exception))

    def test_kind_and_source_are_required(self):
        payload = stub_payload()
        payload.pop("kind")
        with self.assertRaises(EvidenceError) as caught:
            parse_evidence(payload)
        self.assertIn("EVIDENCE_KIND_MISSING", str(caught.exception))
        payload = stub_payload()
        payload["source"] = ""
        with self.assertRaises(EvidenceError) as caught:
            parse_evidence(payload)
        self.assertIn("EVIDENCE_SOURCE_MISSING", str(caught.exception))

    def test_unknown_section_field_is_refused(self):
        payload = stub_payload()
        payload["sections"]["knock"]["whatever"] = 1
        with self.assertRaises(EvidenceError) as caught:
            parse_evidence(payload)
        self.assertIn("unknown field", str(caught.exception))

    def test_real_evidence_must_name_its_channels(self):
        payload = real_payload(rpm_limit={"validated": True, "max_safe_rpm": 6800})
        payload["channels"] = []
        with self.assertRaises(EvidenceError) as caught:
            parse_evidence(payload)
        self.assertIn("EVIDENCE_CHANNELS_MISSING", str(caught.exception))

    def test_real_evidence_without_values_is_refused(self):
        with self.assertRaises(EvidenceError) as caught:
            parse_evidence(real_payload())
        self.assertIn("EVIDENCE_EMPTY", str(caught.exception))

    def test_real_evidence_reports_per_section_status(self):
        evidence = parse_evidence(real_payload(
            rpm_limit={"validated": True, "max_safe_rpm": 6800}))
        self.assertFalse(evidence.is_stub)
        self.assertEqual(evidence.status("rpm_limit"), STATUS_AVAILABLE)
        self.assertEqual(evidence.status("knock"), STATUS_MISSING)
        self.assertEqual(evidence.require("rpm_limit", "validated", "max_safe_rpm"),
                         {"validated": True, "max_safe_rpm": 6800})

    def test_partially_filled_section_counts_as_missing(self):
        # A section that carries only half of its fields cannot answer anything:
        # "max_safe_rpm" without the boolean says nothing about the engine state.
        evidence = parse_evidence(real_payload(rpm_limit={"max_safe_rpm": 6800}))
        self.assertIsNone(evidence.section("rpm_limit"))
        self.assertIsNone(evidence.require("rpm_limit", "validated", "max_safe_rpm"))
        self.assertEqual(evidence.status("rpm_limit"), STATUS_MISSING)


class CliTests(unittest.TestCase):
    def run_tool(self, *arguments):
        return subprocess.run([sys.executable, str(TOOL), *arguments],
                              capture_output=True, text=True)

    def test_write_stub_then_check(self):
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "evidence.json"
            written = self.run_tool("--write-stub", str(target))
            self.assertEqual(written.returncode, 0, written.stderr)
            checked = self.run_tool("--check", str(target))
            self.assertEqual(checked.returncode, 0, checked.stderr)
            self.assertIn("STUB", checked.stdout)

    def test_a_stub_with_data_fails_the_check(self):
        payload = stub_payload()
        payload["sections"]["maf"]["voltage_max_v"] = 4.9
        with tempfile.TemporaryDirectory() as folder:
            target = Path(folder) / "evidence.json"
            target.write_text(json.dumps(payload), encoding="utf-8")
            checked = self.run_tool("--check", str(target))
            self.assertEqual(checked.returncode, 1)
            self.assertIn("STUB_WITH_DATA", checked.stderr)

    def test_json_output_lists_every_section(self):
        completed = self.run_tool("--check", str(STUB_FILE), "--json")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(set(payload["status"]), set(log_evidence.SECTIONS))
        self.assertTrue(all(value == STATUS_STUB for value in payload["status"].values()))


class GeneratorIntegrationTests(unittest.TestCase):
    """The contract only matters if a decision actually reads it."""

    @classmethod
    def setUpClass(cls):
        cls.request = {"version": 1, "intents": [
            {"id": "rpm", "kind": "rpm_limit", "value_rpm": 6700,
             "reason": "клиент просит отсечку выше стока"},
        ]}
        fixtures = sorted((ROOT / "MS41" / "firmware").glob("*partial.bin"))
        cls.bin_path = fixtures[0]
        from MS41.tools.calibration import DEFAULT_POLICY, Policy
        cls.policy = Policy.load(DEFAULT_POLICY)

    def rpm_outcome(self, evidence):
        result = generate(self.bin_path, self.request, self.policy, evidence=evidence)
        return result, next(o for o in result.outcomes if o.kind == "rpm_limit")

    def test_stub_leaves_the_raise_blocked(self):
        result, outcome = self.rpm_outcome(load_evidence(STUB_FILE))
        self.assertEqual(outcome.status, "REQUIRES_DATA")
        self.assertNotIn("c_n_max", [change["symbol"] for change in result.patches])
        self.assertIn("log evidence: STUB", outcome.detail)

    def test_log_evidence_replaces_the_risk_acknowledgement(self):
        evidence = parse_evidence(real_payload(
            rpm_limit={"validated": True, "max_safe_rpm": 6800}))
        result, outcome = self.rpm_outcome(evidence)
        self.assertEqual(outcome.status, "READY", outcome.detail)
        change = outcome.changes[0]
        self.assertEqual(change["symbol"], "c_n_max")
        self.assertEqual(change["evidence"], "log_evidence:test0_2026-05-01.csv")
        self.assertEqual(change["found"], "max_safe_rpm=6800")
        self.assertIn("LOG_BACKED", [finding["code"] for finding in outcome.findings])
        self.assertNotIn("RISK_ACKNOWLEDGED",
                         [finding["code"] for finding in outcome.findings])
        self.assertEqual(result.status, "READY")

    def test_low_logged_ceiling_does_not_block_the_request(self):
        # The contract reports what the log says, it does not overrule the customer:
        # a logged ceiling below the request still plans, and the value is visible
        # in the patch file so the delivery note can carry it.
        evidence = parse_evidence(real_payload(
            rpm_limit={"validated": True, "max_safe_rpm": 8000}))
        result, outcome = self.rpm_outcome(evidence)
        self.assertEqual(outcome.status, "READY")
        self.assertEqual(result.patches[0]["found"], "max_safe_rpm=8000")

    def test_acknowledgement_still_works_without_any_evidence(self):
        request = copy.deepcopy(self.request)
        request["intents"][0]["acknowledge_risk"] = True
        result = generate(self.bin_path, request, self.policy)
        outcome = next(o for o in result.outcomes if o.kind == "rpm_limit")
        self.assertEqual(outcome.status, "READY")
        self.assertIn("RISK_ACKNOWLEDGED",
                      [finding["code"] for finding in outcome.findings])

    def test_stub_is_announced_in_the_notes(self):
        result, _ = self.rpm_outcome(load_evidence(STUB_FILE))
        notes = " ".join(result.notes)
        self.assertIn("stub", notes.lower())
        self.assertIn("REQUIRES_DATA", notes)

    def test_patch_file_carries_the_log_reference(self):
        from MS41.tools.generate_patches import patches_payload
        evidence = parse_evidence(real_payload(
            rpm_limit={"validated": True, "max_safe_rpm": 6800}))
        result, _ = self.rpm_outcome(evidence)
        payload = patches_payload(result, self.request, None)
        entry = next(p for p in payload["patches"] if p["symbol"] == "c_n_max")
        self.assertEqual(entry["evidence"], "log_evidence:test0_2026-05-01.csv")
        self.assertEqual(entry["found"], "max_safe_rpm=6800")


if __name__ == "__main__":
    unittest.main()
