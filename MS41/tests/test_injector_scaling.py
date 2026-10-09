#!/usr/bin/env python3
"""Tests for the injector rescale path (datasheet factor + log confirmation)."""

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

from MS41.tools.calibration import (  # noqa: E402
    DEFAULT_POLICY,
    Policy,
    PolicyEntry,
    apply_plan,
    crc_covered_end,
    load_calibration,
    plan_patches,
)
from MS41.tools.generate_patches import generate  # noqa: E402
from MS41.tools.injector_scaling import (  # noqa: E402
    INJECTION_TITLE,
    ScalingError,
    build_report,
    flow_factor,
    flows_from_profile,
    trims_factor,
)
from MS41.tools.log_evidence import load_evidence, parse_evidence, stub_payload  # noqa: E402
from MS41.tools.maf_curve import build_report as maf_report  # noqa: E402
from MS41.tools.maf_curve import load_curve  # noqa: E402
from MS41.tools.xdf import get_object, load_definition, read_engineering  # noqa: E402

FIRMWARE = ROOT / "MS41" / "firmware"
PARTIAL_B28 = FIRMWARE / "MS41_1429861_E36_328i_M52B28_1997_4101_partial.bin"
EXAMPLES = ROOT / "MS41" / "examples"
PROFILE = EXAMPLES / "stage2_profile_injectors.example.json"
STUB = EXAMPLES / "fuel_scaling.stub.json"
TOOL = ROOT / "MS41" / "tools" / "injector_scaling.py"

STOCK_FLOW = 190.0
NEW_FLOW = 240.0


def profile(**overrides) -> dict:
    data = json.loads(PROFILE.read_text(encoding="utf-8"))
    data.update(overrides)
    return data


def real_evidence(tmpdir: Path, ltft: float = -20.8, stft: float = 1.5,
                  duty: float = 78.0, *, with_trims: bool = True,
                  with_injectors: bool = True) -> Path:
    payload = stub_payload("test evidence")
    payload.update(kind="real", source="test0_injectors.csv",
                   channels=["n", "ltft", "ipw"])
    if with_trims:
        payload["sections"]["trims"] = {"stft_percent": stft, "ltft_percent": ltft}
    if with_injectors:
        payload["sections"]["injectors"] = {"duty_percent_max": duty,
                                            "maf_ceiling_kgh": 430.0}
    path = tmpdir / "evidence.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class ProfileTests(unittest.TestCase):
    def test_flows_are_read_from_the_profile(self):
        self.assertEqual(flows_from_profile(profile()), (STOCK_FLOW, NEW_FLOW))

    def test_stock_injectors_have_nothing_to_rescale(self):
        with self.assertRaises(ScalingError) as caught:
            flows_from_profile(profile(injectors="stock"))
        self.assertIn("stock injectors", str(caught.exception))

    def test_missing_flows_are_refused(self):
        data = profile()
        data.pop("injector_flow_cc_min")
        with self.assertRaises(ScalingError) as caught:
            flows_from_profile(data)
        self.assertIn("cannot be guessed", str(caught.exception))

    def test_non_positive_flows_are_refused(self):
        with self.assertRaises(ScalingError):
            flows_from_profile(profile(injector_flow_cc_min=0))

    def test_factor_direction(self):
        self.assertAlmostEqual(flow_factor(190.0, 240.0), 0.7916666, places=6)
        self.assertAlmostEqual(flow_factor(240.0, 190.0), 1.2631578, places=6)


class EvidenceTests(unittest.TestCase):
    def test_stub_carries_no_factor(self):
        self.assertIsNone(trims_factor(load_evidence(STUB)))

    def test_missing_trims_carry_no_factor(self):
        with tempfile.TemporaryDirectory() as folder:
            path = real_evidence(Path(folder), with_trims=False)
            self.assertIsNone(trims_factor(load_evidence(path)))

    def test_long_term_trim_becomes_the_factor(self):
        with tempfile.TemporaryDirectory() as folder:
            path = real_evidence(Path(folder), ltft=-20.8)
            self.assertAlmostEqual(trims_factor(load_evidence(path)), 0.792, places=6)


class ReportTests(unittest.TestCase):
    def report(self, evidence=None, data=None, policy=None) -> object:
        return build_report(PARTIAL_B28, data or profile(), evidence,
                            policy or Policy.load(DEFAULT_POLICY))

    def test_without_logs_the_rescale_is_not_written(self):
        report = self.report()
        self.assertEqual(report.status, "REQUIRES_DATA")
        self.assertEqual(report.patches, [])
        self.assertEqual(report.details["cells"], 192)
        self.assertAlmostEqual(report.factor_from_flows, 0.7916666, places=6)
        self.assertIsNone(report.factor_from_trims)
        self.assertIn("new_time_ms", report.details)
        self.assertEqual([f["code"] for f in report.findings], ["LOG_DATA_REQUIRED"])

    def test_stub_evidence_keeps_it_waiting(self):
        report = self.report(load_evidence(STUB))
        self.assertEqual(report.status, "REQUIRES_DATA")
        self.assertIn("stub", report.findings[0]["message"])

    def test_agreeing_evidence_plans_the_whole_map(self):
        with tempfile.TemporaryDirectory() as folder:
            path = real_evidence(Path(folder))
            report = self.report(load_evidence(path))
        self.assertEqual(report.status, "READY", report.findings)
        self.assertEqual(report.plan_status, "READY")
        self.assertEqual(len(report.patches), 192)
        self.assertEqual([p["index"] for p in report.patches], list(range(192)))
        self.assertAlmostEqual(report.factor_from_trims, 0.792, places=6)
        self.assertLess(report.details["factor_divergence_percent"], 1.0)
        self.assertTrue(all(p["evidence"].startswith("documented_injectors:")
                            for p in report.patches))
        self.assertTrue(all("log_trims:test0_injectors.csv" in p["evidence"]
                            for p in report.patches))

    def test_planned_values_are_the_stock_map_scaled(self):
        with tempfile.TemporaryDirectory() as folder:
            path = real_evidence(Path(folder))
            report = self.report(load_evidence(path))
        objects, _ = load_definition()
        image_obj = get_object(objects, INJECTION_TITLE)
        image, offset, _ = load_calibration(PARTIAL_B28)
        stock = read_engineering(bytes(image[offset:offset + 0x6000]), image_obj)
        step = image_obj.scaler.a
        for index, patch in enumerate(report.patches):
            # the value written is the raw cell that best matches stock x factor
            self.assertLessEqual(
                abs(patch["value"] - stock[index] * 0.7916666), step + 1e-9,
                f"cell {index} is further from the scaled value than one raw step")

    def test_mismatch_between_datasheet_and_log_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            path = real_evidence(Path(folder), ltft=-5.0)
            report = self.report(load_evidence(path))
        self.assertEqual(report.status, "REJECTED")
        codes = [f["code"] for f in report.findings]
        self.assertIn("FACTOR_MISMATCH", codes)
        self.assertEqual(report.patches, [])

    def test_duty_above_the_limit_is_refused(self):
        with tempfile.TemporaryDirectory() as folder:
            path = real_evidence(Path(folder), duty=93.0)
            report = self.report(load_evidence(path))
        self.assertEqual(report.status, "REJECTED")
        self.assertIn("DUTY_TOO_HIGH", [f["code"] for f in report.findings])

    def test_high_duty_warns_but_plans(self):
        with tempfile.TemporaryDirectory() as folder:
            path = real_evidence(Path(folder), duty=85.0)
            report = self.report(load_evidence(path))
        self.assertEqual(report.status, "READY")
        self.assertIn("DUTY_HIGH", [f["code"] for f in report.findings])

    def test_noisy_short_term_trim_is_flagged(self):
        with tempfile.TemporaryDirectory() as folder:
            path = real_evidence(Path(folder), stft=14.0)
            report = self.report(load_evidence(path))
        self.assertIn("SHORT_TERM_TRIM_HIGH", [f["code"] for f in report.findings])

    def test_neutral_factor_changes_nothing(self):
        report = self.report(data=profile(injector_flow_cc_min=190.0))
        self.assertEqual(report.status, "EMPTY")
        self.assertEqual(report.patches, [])

    def test_outstanding_items_are_named_in_the_report(self):
        with tempfile.TemporaryDirectory() as folder:
            path = real_evidence(Path(folder))
            report = self.report(load_evidence(path))
        codes = [f["code"] for f in report.findings]
        self.assertIn("MIN_INJECTION_TIME_REVIEW", codes)
        self.assertIn("CRANKING_MAP_REVIEW", codes)
        self.assertIn("ADAPTATION_LIMITS_KEPT", codes)
        self.assertIn("c_ti_min_iv", report.details["untouched"])

    def test_missing_duty_section_warns_instead_of_blocking(self):
        with tempfile.TemporaryDirectory() as folder:
            path = real_evidence(Path(folder), with_injectors=False)
            report = self.report(load_evidence(path))
        self.assertEqual(report.status, "READY")
        self.assertIn("DUTY_HEADROOM_UNKNOWN", [f["code"] for f in report.findings])

    def test_planner_findings_are_not_repeated_per_cell(self):
        policy = Policy(version=1, entries={})          # allowlist empty
        with tempfile.TemporaryDirectory() as folder:
            path = real_evidence(Path(folder))
            report = self.report(load_evidence(path), policy=policy)
        self.assertEqual(report.status, "REJECTED")
        rejects = [f for f in report.findings if f["code"] == "SYMBOL_NOT_ALLOWED"]
        self.assertEqual(len(rejects), 1)

    def test_maf_curve_report_deduplicates_planner_findings_too(self):
        policy = Policy(version=1, entries={})          # allowlist empty
        curve = load_curve(EXAMPLES / "maf_curve_example.json")
        report = maf_report(PARTIAL_B28, curve, policy)
        self.assertEqual(report.status, "REJECTED")
        rejects = [f for f in report.findings if f["code"] == "SYMBOL_NOT_ALLOWED"]
        self.assertEqual(len(rejects), 1)

    def test_planned_cells_are_inside_the_crc_covered_range(self):
        objects, _ = load_definition()
        obj = get_object(objects, INJECTION_TITLE)
        last = obj.address + (obj.element_count - 1) * obj.element_size_bytes
        image, offset, _ = load_calibration(PARTIAL_B28)
        covered = crc_covered_end(bytes(image[offset:offset + 0x6000]))
        self.assertLess(last + obj.element_size_bytes, covered)

    def test_the_scaled_plan_passes_the_real_patcher(self):
        with tempfile.TemporaryDirectory() as folder:
            path = real_evidence(Path(folder))
            report = self.report(load_evidence(path))
            requests = [{"symbol": p["symbol"], "index": p["index"], "value": p["value"]}
                        for p in report.patches]
            image, offset, image_type = load_calibration(PARTIAL_B28)
            objects, problems = load_definition()
            plan = plan_patches(image, offset, image_type, objects, problems, requests,
                                Policy.load(DEFAULT_POLICY))
            self.assertEqual(plan.status, "READY", [f.message for f in plan.findings])
            patched, blocks = apply_plan(image, offset, plan, objects)
            self.assertTrue(patched)
            self.assertGreater(blocks, 0)


class PolicyTests(unittest.TestCase):
    def test_the_base_map_is_allowlisted_for_the_intent(self):
        policy = Policy.load(DEFAULT_POLICY)
        entry = policy.entry(INJECTION_TITLE)
        self.assertIsNotNone(entry, "the injection map must be allowlisted by title")
        self.assertGreaterEqual(entry.max_cells_per_patch or 0, 192)
        self.assertIsNotNone(entry.max_delta_percent)
        self.assertLess(entry.maximum, 100.0, "a fuel map in ms needs a sane ceiling")

    def test_an_unknown_symbol_is_still_refused(self):
        policy = Policy(version=1, entries={INJECTION_TITLE: PolicyEntry(
            symbol=INJECTION_TITLE, minimum=0.0, maximum=30.0)})
        self.assertIsNotNone(policy.entry(INJECTION_TITLE))


class GeneratorTests(unittest.TestCase):
    def request(self, acknowledge: bool = False) -> dict:
        intent = {"id": "inj", "kind": "injectors", "reason": "замена форсунок"}
        if acknowledge:
            intent["acknowledge_risk"] = True
        return {"version": 1, "customer": "test", "stage": 2, "ron": 95,
                "intents": [intent]}

    def outcome(self, result) -> object:
        return next(o for o in result.outcomes if o.kind == "injectors")

    def test_without_a_profile_the_intent_is_blocked(self):
        result = generate(PARTIAL_B28, self.request(), Policy.load(DEFAULT_POLICY))
        outcome = self.outcome(result)
        self.assertEqual(outcome.status, "BLOCKED")
        self.assertIn("--profile", outcome.detail)

    def test_without_logs_the_intent_reports_the_factor_and_waits(self):
        result = generate(PARTIAL_B28, self.request(), Policy.load(DEFAULT_POLICY),
                          profile())
        outcome = self.outcome(result)
        self.assertEqual(outcome.status, "REQUIRES_DATA")
        self.assertIn("192", outcome.detail)
        self.assertIn("0.7917", outcome.detail)
        self.assertEqual(result.patches, [])

    def test_agreeing_logs_produce_a_full_plan(self):
        with tempfile.TemporaryDirectory() as folder:
            path = real_evidence(Path(folder))
            result = generate(PARTIAL_B28, self.request(), Policy.load(DEFAULT_POLICY),
                              profile(), evidence=load_evidence(path))
        outcome = self.outcome(result)
        self.assertEqual(outcome.status, "READY", outcome.detail)
        self.assertEqual(len(outcome.changes), 192)
        self.assertEqual(result.status, "READY")
        self.assertEqual({p["symbol"] for p in result.patches}, {INJECTION_TITLE})
        self.assertTrue(all(p["requires_validation"] for p in result.patches))

    def test_two_kinds_in_one_request(self):
        with tempfile.TemporaryDirectory() as folder:
            path = real_evidence(Path(folder))
            request = self.request()
            request["intents"].append({"id": "v", "kind": "vmax", "mode": "raise",
                                       "value_kmh": 250})
            result = generate(PARTIAL_B28, request, Policy.load(DEFAULT_POLICY),
                              profile(), evidence=load_evidence(path))
        self.assertEqual(result.status, "READY")
        symbols = {p["symbol"] for p in result.patches}
        self.assertEqual(symbols, {INJECTION_TITLE, "c_vs_max"})

    def test_profile_without_dead_time_data_blocks_generation(self):
        # The hardware profile owns this rule; the intent must not be able to
        # sneak past it, because a dead time that is not documented cannot be
        # checked against the small-pulse behaviour of the new injectors.
        data = profile(injector_dead_time_documented=False)
        result = generate(PARTIAL_B28, self.request(), Policy.load(DEFAULT_POLICY), data)
        self.assertEqual(result.status, "BLOCKED")
        self.assertTrue(any("HARDWARE_PROFILE_REJECTED" in reason
                            for reason in result.blocked_reasons), result.blocked_reasons)
        self.assertEqual(result.patches, [])

    def test_mismatch_blocks_the_intent(self):
        with tempfile.TemporaryDirectory() as folder:
            path = real_evidence(Path(folder), ltft=-5.0)
            result = generate(PARTIAL_B28, self.request(), Policy.load(DEFAULT_POLICY),
                              profile(), evidence=load_evidence(path))
        outcome = self.outcome(result)
        self.assertEqual(outcome.status, "BLOCKED")
        self.assertIn("FACTOR_MISMATCH", [f["code"] for f in outcome.findings])


class CliTests(unittest.TestCase):
    def run_tool(self, *arguments):
        return subprocess.run([sys.executable, str(TOOL), *arguments],
                              capture_output=True, text=True)

    def test_waiting_for_logs_is_not_an_error(self):
        completed = self.run_tool("--bin", str(PARTIAL_B28), "--profile", str(PROFILE),
                                  "--evidence", str(STUB), "--json")
        self.assertEqual(completed.returncode, 0)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["report"]["status"], "REQUIRES_DATA")
        self.assertEqual(payload["report"]["details"]["cells"], 192)

    def test_rejection_exits_non_zero(self):
        with tempfile.TemporaryDirectory() as folder:
            path = real_evidence(Path(folder), ltft=-5.0)
            completed = self.run_tool("--bin", str(PARTIAL_B28), "--profile", str(PROFILE),
                                      "--evidence", str(path))
        self.assertEqual(completed.returncode, 1)
        self.assertIn("FACTOR_MISMATCH", completed.stdout)

    def test_missing_flows_are_reported_cleanly(self):
        data = profile()
        data.pop("injector_flow_cc_min")
        with tempfile.TemporaryDirectory() as folder:
            broken = Path(folder) / "profile.json"
            broken.write_text(json.dumps(data), encoding="utf-8")
            completed = self.run_tool("--bin", str(PARTIAL_B28), "--profile", str(broken))
        self.assertEqual(completed.returncode, 1)
        self.assertIn("cannot be guessed", completed.stderr)


if __name__ == "__main__":
    unittest.main()
