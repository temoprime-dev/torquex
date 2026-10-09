#!/usr/bin/env python3
"""Tests for aligning the air-mass limits with a replacement MAF curve."""

from __future__ import annotations

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
    load_calibration,
    plan_patches,
)
from MS41.tools.generate_patches import generate  # noqa: E402
from MS41.tools.log_evidence import load_evidence, stub_payload  # noqa: E402
from MS41.tools.maf_curve import Curve, parse_curve  # noqa: E402
from MS41.tools.maf_limits import (  # noqa: E402
    LIMIT_TITLES,
    REVIEWED,
    STATUS_EMPTY,
    STATUS_READY,
    STATUS_REJECTED,
    STATUS_REQUIRES_DATA,
    build_report,
    ceiling_ratio,
    saturation_proof,
)
from MS41.tools.xdf import get_object, load_definition  # noqa: E402

FIRMWARE = ROOT / "MS41" / "firmware"
PARTIAL_B28 = FIRMWARE / "MS41_1429861_E36_328i_M52B28_1997_4101_partial.bin"
B20_PARTIAL = FIRMWARE / "MS41_1429861_E36_320i_M52B20_1996_4101_partial.bin"
EXAMPLES = ROOT / "MS41" / "examples"
CURVE_6_PERCENT = EXAMPLES / "maf_curve_example.json"
CURVE_STOCK = EXAMPLES / "maf_curve_stock_b28.example.json"
STUB = EXAMPLES / "log_evidence.stub.json"
REQUEST = EXAMPLES / "stage2_request_maf.example.json"
TOOL = ROOT / "MS41" / "tools" / "maf_limits.py"

STOCK_CEILING = 725.09375
NEW_CEILING = 768.5994


def scaled_curve(factor: float, source: str = "test curve") -> Curve:
    stock = json.loads(CURVE_STOCK.read_text(encoding="utf-8"))
    points = [[v, flow * factor] for v, flow in stock["points"]]
    return parse_curve({"sensor": "test sensor", "source": source,
                        "provenance": "derived", "units": "kg/h", "points": points})


def ramp_curve(ceiling: float, source: str = "test curve") -> Curve:
    """A monotonic transfer function that ends at the given ceiling."""
    return parse_curve({"sensor": "test sensor", "source": source,
                        "provenance": "derived", "units": "kg/h",
                        "points": [[0.0, 0.0], [2.5, round(ceiling * 0.45, 4)],
                                   [5.018, ceiling]]})


def saturation_evidence(tmpdir: Path, *, saturated: bool = True,
                        volts: float = 4.9) -> Path:
    payload = stub_payload("test evidence")
    payload.update(kind="real", source="test0_wot.csv",
                   channels=["n", "maf_v", "ml"])
    payload["sections"]["maf"] = {"voltage_max_v": volts, "saturated": saturated}
    path = tmpdir / "evidence.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class RatioTests(unittest.TestCase):
    def test_the_example_curve_raises_the_ceiling_by_six_percent(self):
        from MS41.tools.maf_curve import load_curve
        ratio, stock, requested = ceiling_ratio(PARTIAL_B28, load_curve(CURVE_6_PERCENT))
        self.assertAlmostEqual(ratio, 1.06, places=6)
        self.assertAlmostEqual(max(stock), STOCK_CEILING, places=4)
        self.assertAlmostEqual(max(requested), NEW_CEILING, places=3)

    def test_a_stock_curve_does_not_move_the_ratio(self):
        from MS41.tools.maf_curve import load_curve
        ratio, _, _ = ceiling_ratio(PARTIAL_B28, load_curve(CURVE_STOCK))
        self.assertAlmostEqual(ratio, 1.0, places=6)


class SaturationProofTests(unittest.TestCase):
    def test_no_evidence_is_not_proof(self):
        proof, reason = saturation_proof(None)
        self.assertIsNone(proof)
        self.assertIn("лога нет", reason)

    def test_a_stub_is_not_proof(self):
        proof, reason = saturation_proof(load_evidence(STUB))
        self.assertIsNone(proof)
        self.assertIn("заглушка", reason)

    def test_saturated_with_a_credible_voltage_is_proof(self):
        with tempfile.TemporaryDirectory() as folder:
            proof, reason = saturation_proof(load_evidence(saturation_evidence(Path(folder))))
        self.assertIsNotNone(proof)
        self.assertEqual(reason, "")
        self.assertTrue(proof["saturated"])

    def test_a_low_voltage_makes_the_claim_implausible(self):
        with tempfile.TemporaryDirectory() as folder:
            path = saturation_evidence(Path(folder), volts=3.6)
            proof, reason = saturation_proof(load_evidence(path))
        self.assertIsNone(proof)
        self.assertIn("неправдоподобна", reason)

    def test_an_unsaturated_log_is_not_proof(self):
        with tempfile.TemporaryDirectory() as folder:
            path = saturation_evidence(Path(folder), saturated=False)
            evidence = load_evidence(path)
        _, reason = saturation_proof(evidence)
        self.assertIn("не насыщался", reason)


class ReportTests(unittest.TestCase):
    def report(self, curve=None, evidence=None, policy=None):
        from MS41.tools.maf_curve import load_curve
        return build_report(PARTIAL_B28, curve or load_curve(CURVE_6_PERCENT),
                            evidence, policy or Policy.load(DEFAULT_POLICY))

    def test_a_neutral_curve_plans_nothing(self):
        from MS41.tools.maf_curve import load_curve
        report = self.report(curve=load_curve(CURVE_STOCK))
        self.assertEqual(report.status, STATUS_EMPTY)
        self.assertEqual(report.patches, [])
        self.assertIn("CEILING_NOT_RAISED", [f["code"] for f in report.findings])

    def test_without_logs_the_limits_stay_where_they_are(self):
        report = self.report()
        self.assertEqual(report.status, STATUS_REQUIRES_DATA)
        self.assertEqual(report.patches, [])
        self.assertAlmostEqual(report.ratio, 1.06, places=6)
        limits = report.details["limits"]
        self.assertAlmostEqual(limits["c_maf_max"]["stock"], 800.7176, places=3)
        self.assertAlmostEqual(limits["c_maf_max"]["aligned"], 848.7607, places=3)
        self.assertEqual(limits["c_maf_max"]["aligned_raw"], 156)
        self.assertAlmostEqual(limits["c_maf_max_diag"]["aligned"], 1062.3943, places=3)
        self.assertEqual(limits["c_maf_max_diag"]["aligned_raw"], 195)

    def test_the_untouched_family_is_listed_with_reasons(self):
        report = self.report()
        self.assertEqual(report.details["reviewed_not_changed"], REVIEWED)
        for title in ("c_maf_max_ch_3", "c_maf_lam_tol_tra", "c_maf_max_isa"):
            self.assertIn(title, report.details["reviewed_not_changed"])
        self.assertNotIn("c_maf_max_ch_3", report.details["limits"])

    def test_saturation_proof_plans_both_limits(self):
        with tempfile.TemporaryDirectory() as folder:
            path = saturation_evidence(Path(folder))
            report = self.report(evidence=load_evidence(path))
        self.assertEqual(report.status, STATUS_READY, report.findings)
        self.assertEqual([p["symbol"] for p in report.patches], list(LIMIT_TITLES))
        self.assertEqual(report.plan_status, STATUS_READY)
        for patch in report.patches:
            self.assertTrue(patch["requires_validation"])
            self.assertIn("curve_ceiling_ratio:1.0600", patch["evidence"])
            self.assertIn("log_saturation:test0_wot.csv", patch["evidence"])
            self.assertIn("коэффициент 1.0600", patch["reason"])
        codes = [f["code"] for f in report.findings]
        self.assertIn("LIMITS_ALIGNED", codes)
        self.assertIn("DIAG_LIMIT_FOLLOWS_SENSOR", codes)

    def test_a_ratio_above_the_policy_bound_is_refused(self):
        report = self.report(curve=scaled_curve(1.35))
        self.assertEqual(report.status, STATUS_REJECTED)
        codes = [f["code"] for f in report.findings]
        self.assertIn("CEILING_RISE_OUT_OF_SCOPE", codes)
        self.assertEqual(report.patches, [])

    def test_a_huge_rise_runs_out_of_cell(self):
        # The 8-bit c_maf_max cell tops out at 1389 mg/stroke, i.e. raw 255
        # against a stock raw of 147: the B20 calibration (ceiling 518 kg/h) can
        # be asked for a curve the cell cannot follow, and that is an error, not
        # a silent clamp.
        policy = Policy(version=1, entries={
            "c_maf_max": PolicyEntry(symbol="c_maf_max", minimum=0.0, maximum=1389.0,
                                     max_delta_percent=200.0),
            "c_maf_max_diag": PolicyEntry(symbol="c_maf_max_diag", minimum=0.0,
                                          maximum=1389.0, max_delta_percent=200.0),
        })
        report = build_report(B20_PARTIAL, ramp_curve(518.0 * 1.9), None, policy)
        self.assertEqual(report.status, STATUS_REJECTED)
        self.assertIn("NO_RAW_HEADROOM", [f["code"] for f in report.findings])

    def test_a_symbol_outside_the_allowlist_is_refused(self):
        policy = Policy(version=1, entries={
            "c_maf_max": PolicyEntry(symbol="c_maf_max", minimum=0.0, maximum=1389.0,
                                     max_delta_percent=30.0),
        })
        report = self.report(policy=policy)
        self.assertEqual(report.status, STATUS_REJECTED)
        self.assertIn("LIMIT_NOT_ALLOWED", [f["code"] for f in report.findings])

    def test_the_planned_values_survive_the_real_planner(self):
        with tempfile.TemporaryDirectory() as folder:
            path = saturation_evidence(Path(folder))
            report = self.report(evidence=load_evidence(path))
        objects, problems = load_definition()
        image, offset, image_type = load_calibration(PARTIAL_B28)
        requests = [{"symbol": p["symbol"], "value": p["value"]} for p in report.patches]
        plan = plan_patches(image, offset, image_type, objects, problems, requests,
                            Policy.load(DEFAULT_POLICY))
        self.assertEqual(plan.status, STATUS_READY, [f.message for f in plan.findings])
        patched, blocks = apply_plan(image, offset, plan, objects)
        self.assertEqual(blocks, 15)
        # and the written bytes are the aligned raw values
        self.assertEqual(patched[offset + 0x1B5], 156)
        self.assertEqual(patched[offset + 0x1B6], 195)

    def test_quantization_stays_within_the_policy(self):
        with tempfile.TemporaryDirectory() as folder:
            path = saturation_evidence(Path(folder))
            report = self.report(evidence=load_evidence(path))
        for entry in report.details["limits"].values():
            self.assertLess(entry["quantization_error_percent"], 0.5)


class PolicyTests(unittest.TestCase):
    def test_both_limits_are_allowlisted_with_a_bound(self):
        policy = Policy.load(DEFAULT_POLICY)
        for title in LIMIT_TITLES:
            entry = policy.entry(title)
            self.assertIsNotNone(entry, title)
            self.assertEqual(entry.max_delta_percent, 30.0, title)
            self.assertGreaterEqual(entry.maximum, 1389.0, title)


class GeneratorTests(unittest.TestCase):
    def request(self) -> dict:
        return json.loads(REQUEST.read_text(encoding="utf-8"))

    def test_without_logs_the_curve_is_written_but_the_limits_are_not(self):
        result = generate(PARTIAL_B28, self.request(), Policy.load(DEFAULT_POLICY),
                          None, None, base_dir=REQUEST.parent,
                          evidence=load_evidence(STUB))
        self.assertEqual(result.status, "READY")
        symbols = {p["symbol"] for p in result.patches}
        self.assertIn("id_maf_tab__v_maf_1__v_maf_2 (1x256)", symbols)
        self.assertNotIn("c_maf_max", symbols)
        outcome = next(o for o in result.outcomes if o.kind == "maf_curve")
        codes = [f["code"] for f in outcome.findings]
        self.assertIn("LIMITS_UNCHANGED", codes)
        self.assertIn("SATURATION_NOT_PROVEN", codes)

    def test_saturation_proof_adds_the_limits_to_the_same_plan(self):
        with tempfile.TemporaryDirectory() as folder:
            path = saturation_evidence(Path(folder))
            result = generate(PARTIAL_B28, self.request(), Policy.load(DEFAULT_POLICY),
                              None, None, base_dir=REQUEST.parent,
                              evidence=load_evidence(path))
        self.assertEqual(result.status, "READY")
        symbols = {p["symbol"] for p in result.patches}
        self.assertEqual(symbols, {"id_maf_tab__v_maf_1__v_maf_2 (1x256)",
                                   "c_maf_max", "c_maf_max_diag", "c_vs_max"})
        outcome = next(o for o in result.outcomes if o.kind == "maf_curve")
        self.assertIn("2 air-mass limit(s) aligned", outcome.detail)
        self.assertNotIn("LIMITS_UNCHANGED", [f["code"] for f in outcome.findings])
        limits = [p for p in result.patches if p["symbol"] == "c_maf_max"]
        self.assertEqual(len(limits), 1)
        self.assertTrue(limits[0]["requires_validation"])

    def test_the_generator_patch_file_passes_the_patcher(self):
        from MS41.tools.generate_patches import patches_payload
        with tempfile.TemporaryDirectory() as folder:
            path = saturation_evidence(Path(folder))
            result = generate(PARTIAL_B28, self.request(), Policy.load(DEFAULT_POLICY),
                              None, None, base_dir=REQUEST.parent,
                              evidence=load_evidence(path))
            payload = patches_payload(result, self.request(), None)
            objects, problems = load_definition()
            image, offset, image_type = load_calibration(PARTIAL_B28)
            plan = plan_patches(image, offset, image_type, objects, problems,
                                payload["patches"], Policy.load(DEFAULT_POLICY))
            self.assertEqual(plan.status, STATUS_READY, [f.message for f in plan.findings])
            patched, blocks = apply_plan(image, offset, plan, objects)
        self.assertEqual(blocks, 15)
        self.assertEqual(patched[offset + 0x1B5], 156)
        self.assertEqual(patched[offset + 0x1B6], 195)


class CliTests(unittest.TestCase):
    def run_tool(self, *arguments):
        return subprocess.run([sys.executable, str(TOOL), *arguments],
                              capture_output=True, text=True)

    def test_waiting_for_the_log_is_not_an_error(self):
        completed = self.run_tool("--bin", str(PARTIAL_B28), "--curve",
                                  str(CURVE_6_PERCENT), "--evidence", str(STUB),
                                  "--json")
        self.assertEqual(completed.returncode, 0)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["report"]["status"], STATUS_REQUIRES_DATA)
        self.assertEqual(payload["report"]["details"]["limits"]["c_maf_max"]["aligned_raw"],
                         156)

    def test_a_proven_raise_writes_the_patch_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = saturation_evidence(Path(folder))
            out = Path(folder) / "patches.json"
            completed = self.run_tool("--bin", str(PARTIAL_B28), "--curve",
                                      str(CURVE_6_PERCENT), "--evidence", str(path),
                                      "--out", str(out))
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("2 limit(s) planned", completed.stdout)
            payload = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual([p["symbol"] for p in payload["patches"]], list(LIMIT_TITLES))

    def test_an_out_of_scope_rise_exits_non_zero(self):
        with tempfile.TemporaryDirectory() as folder:
            curve = Path(folder) / "curve.json"
            curve.write_text(json.dumps(scaled_curve(1.35).to_json()), encoding="utf-8")
            completed = self.run_tool("--bin", str(PARTIAL_B28), "--curve", str(curve))
        self.assertEqual(completed.returncode, 1)
        self.assertIn("CEILING_RISE_OUT_OF_SCOPE", completed.stdout)


class DryRunReportTests(unittest.TestCase):
    def test_a_dry_run_does_not_claim_the_checksum_failed(self):
        completed = subprocess.run(
            [sys.executable, str(ROOT / "MS41" / "tools" / "calibration.py"),
             "--bin", str(PARTIAL_B28), "--patches", str(ROOT / "MS41" / "examples" /
                                                           "patches_hardcut.example.json"),
             "--json"], capture_output=True, text=True)
        payload = json.loads(completed.stdout)
        self.assertTrue(payload["dry_run"])
        self.assertIsNone(payload["crc_valid_after"])


if __name__ == "__main__":
    unittest.main()
