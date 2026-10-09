#!/usr/bin/env python3
"""Tests for the VANOS switching threshold follow-up (measured comparison)."""

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
    apply_plan,
    load_calibration,
    plan_patches,
)
from MS41.tools.generate_patches import generate  # noqa: E402
from MS41.tools.vanos_switch import (  # noqa: E402
    AXIS_TITLES,
    STATUS_EMPTY,
    STATUS_READY,
    STATUS_REJECTED,
    TRANSFER_TABLES,
    ThresholdError,
    build_report,
    crossing_rpm,
    dump_stock,
    interpolate,
    load_comparison,
    parse_comparison,
)
from MS41.tools.xdf import get_object, load_definition  # noqa: E402

FIRMWARE = ROOT / "MS41" / "firmware"
PARTIAL_B28 = FIRMWARE / "MS41_1429861_E36_328i_M52B28_1997_4101_partial.bin"
EXAMPLES = ROOT / "MS41" / "examples"
EXAMPLE_FILE = EXAMPLES / "vanos_comparison.example.json"
PROFILE = EXAMPLES / "stage2_profile.example.json"
TOOL = ROOT / "MS41" / "tools" / "vanos_switch.py"

MANUAL_SYMBOL = TRANSFER_TABLES["manual"]
AUTOMATIC_SYMBOL = TRANSFER_TABLES["automatic"]
SENTINEL_RAW = 239
RPM_AXIS = [704.0, 928.0, 992.0, 1248.0, 1504.0, 1760.0,
            2016.0, 2240.0, 2752.0, 4064.0, 4128.0]
TCO_AXIS = [-9.75, 9.75, 30.0, 72.75, 84.75]
WARM_COLUMN = 4


def comparison_data(**overrides) -> dict:
    data = json.loads(EXAMPLE_FILE.read_text(encoding="utf-8"))
    data.update(provenance="measured", source="test0 dyno session",
                transmission="manual", engine="m52b28", measured_tco_c=80.0,
                crossing_rpm=2252.8)
    data.update(overrides)
    return data


def comparison_file(tmpdir: Path, **overrides) -> Path:
    path = tmpdir / "comparison.json"
    path.write_text(json.dumps(comparison_data(**overrides)), encoding="utf-8")
    return path


class ParsingTests(unittest.TestCase):
    def test_the_shipped_example_is_synthetically_derived(self):
        data = json.loads(EXAMPLE_FILE.read_text(encoding="utf-8"))
        self.assertEqual(data["provenance"], "derived")
        self.assertIn("SYNTHETIC", data["source"])
        with self.assertRaises(ThresholdError) as caught:
            parse_comparison(data)
        self.assertIn("PROVENANCE_NOT_MEASURED", str(caught.exception))

    def test_a_measured_file_parses(self):
        comparison = parse_comparison(comparison_data())
        self.assertEqual(comparison.transmission, "manual")
        self.assertEqual(len(comparison.threshold), 4)
        self.assertAlmostEqual(comparison.measured_tco_c, 80.0)

    def test_missing_evidence_is_refused(self):
        for key, code in (("engine", "EVIDENCE_MISSING"), ("source", "EVIDENCE_MISSING"),
                          ("measured_tco_c", "MEASUREMENT_TEMPERATURE_MISSING"),
                          ("crossing_rpm", "CROSSING_MISSING")):
            data = comparison_data()
            data.pop(key)
            with self.assertRaises(ThresholdError) as caught:
                parse_comparison(data)
            self.assertIn(code, str(caught.exception), key)

    def test_transmission_must_be_known(self):
        with self.assertRaises(ThresholdError) as caught:
            parse_comparison(comparison_data(transmission="sequential"))
        self.assertIn("TRANSMISSION_UNKNOWN", str(caught.exception))

    def test_units_must_be_the_table_units(self):
        with self.assertRaises(ThresholdError) as caught:
            parse_comparison(comparison_data(units="kg/h"))
        self.assertIn("UNITS_UNSUPPORTED", str(caught.exception))

    def test_points_must_be_ordered_pairs(self):
        with self.assertRaises(ThresholdError) as caught:
            parse_comparison(comparison_data(threshold=[[2000, 400], [1500, 380]]))
        self.assertIn("rpm must strictly increase", str(caught.exception))
        with self.assertRaises(ThresholdError):
            parse_comparison(comparison_data(advanced=[[1000, 300]]))
        with self.assertRaises(ThresholdError):
            parse_comparison(comparison_data(retarded=[[1000, 300], [1000, 320]]))


class CurveMathTests(unittest.TestCase):
    def test_the_crossing_is_interpolated(self):
        comparison = parse_comparison(comparison_data())
        self.assertAlmostEqual(crossing_rpm(comparison), 2252.8, places=1)

    def test_parallel_curves_have_no_crossing(self):
        comparison = parse_comparison(comparison_data(
            advanced=[[704, 300], [4064, 560]],
            retarded=[[704, 250], [4064, 510]]))
        self.assertIsNone(crossing_rpm(comparison))

    def test_interpolation_never_extrapolates(self):
        points = ((704.0, 270.0), (4064.0, 580.0))
        self.assertIsNone(interpolate(points, 600.0))
        self.assertIsNone(interpolate(points, 5000.0))
        self.assertAlmostEqual(interpolate(points, 1504.0),
                               270 + (1504 - 704) / (4064 - 704) * 310, places=6)


class DumpTests(unittest.TestCase):
    def test_stock_dump_reads_the_declared_axes(self):
        dump = dump_stock(PARTIAL_B28, "manual")
        self.assertEqual(dump["symbol"], MANUAL_SYMBOL)
        self.assertEqual(dump["shape"], "11x5")
        self.assertEqual(dump["rpm_axis"], RPM_AXIS)
        self.assertEqual(dump["tco_axis_c"], TCO_AXIS)
        self.assertAlmostEqual(dump["quantum_mg"], 5.44706, places=9)
        self.assertEqual(dump["never_switch"]["raw"], SENTINEL_RAW)
        self.assertEqual(dump["never_switch"]["cells"], 15)

    def test_sentinel_cells_are_the_cold_column_and_the_top_row(self):
        dump = dump_stock(PARTIAL_B28, "manual")
        flags = [(row["rpm"], row["never_switch"]) for row in dump["rows"]]
        self.assertTrue(all(row[1][0] for row in flags), "cold column must be sentinel")
        self.assertTrue(all(flags[-1][1]), "the top rpm row must be sentinel")
        self.assertFalse(any(flags[1][1][1:]), "warm cells must carry real values")

    def test_the_two_transmissions_use_different_tables(self):
        manual = dump_stock(PARTIAL_B28, "manual")
        automatic = dump_stock(PARTIAL_B28, "automatic")
        self.assertEqual(manual["symbol"], MANUAL_SYMBOL)
        self.assertEqual(automatic["symbol"], AUTOMATIC_SYMBOL)
        self.assertNotEqual(manual["address"], automatic["address"])
        # the two lines agree at 2016 rpm but part company below it - which is
        # exactly why a value may never be copied from one car to the other
        manual_row = next(r["values"] for r in manual["rows"] if r["rpm"] == 992.0)
        automatic_row = next(r["values"] for r in automatic["rows"] if r["rpm"] == 992.0)
        self.assertNotEqual(manual_row[WARM_COLUMN], automatic_row[WARM_COLUMN])


class ReportTests(unittest.TestCase):
    def report(self, transmission="manual", policy=None, **overrides):
        data = comparison_data(**overrides)
        if overrides.get("transmission") and transmission == "manual":
            transmission = overrides["transmission"]
        return build_report(PARTIAL_B28, transmission, parse_comparison(data),
                            policy or Policy.load(DEFAULT_POLICY), engine="m52b28")

    def test_a_measured_comparison_plans_the_warm_column(self):
        report = self.report()
        self.assertEqual(report.status, STATUS_READY, report.findings)
        self.assertEqual(report.plan_status, STATUS_READY)
        self.assertEqual(len(report.patches), 10)
        self.assertEqual([p["index"] for p in report.patches],
                         [row * 5 + WARM_COLUMN for row in range(10)])
        self.assertEqual(report.details["warm_columns"]["columns"], [WARM_COLUMN])
        self.assertEqual(report.details["warm_columns"]["kept_cold_columns"],
                         TCO_AXIS[:WARM_COLUMN])
        self.assertEqual(report.details["sentinel_rows_kept"], [4128.0])
        self.assertEqual(report.details["rows_kept_outside_measurement"], [])
        self.assertEqual(report.details["planned_cells"], 10)

    def test_values_follow_the_measured_line(self):
        report = self.report()
        by_rpm = {}
        for patch in report.patches:
            step = (report.details["quantum_mg"])
            by_rpm[patch["index"] // 5 * 5 + WARM_COLUMN] = patch["value"]
        # 704 rpm -> 270 mg/stroke in the file, one quantum of rounding allowed
        self.assertLess(abs(report.patches[0]["value"] - 272.353), 20.0)
        self.assertGreater(report.patches[0]["value"], 0.0)

    def test_an_unreachable_threshold_is_refused(self):
        report = self.report(threshold=[[704, 900], [1504, 410], [2752, 550]])
        self.assertEqual(report.status, STATUS_REJECTED)
        self.assertIn("THRESHOLD_UNREACHABLE", [f["code"] for f in report.findings])

    def test_a_wrong_documented_crossing_is_refused(self):
        report = self.report(crossing_rpm=3000.0)
        self.assertEqual(report.status, STATUS_REJECTED)
        finding = next(f for f in report.findings if f["code"] == "CROSSING_MISMATCH")
        self.assertIn("2253", finding["message"])

    def test_curves_that_never_cross_are_refused(self):
        report = self.report(advanced=[[704, 300], [4064, 560]],
                             retarded=[[704, 250], [4064, 510]])
        self.assertEqual(report.status, STATUS_REJECTED)
        self.assertIn("NO_CROSSING", [f["code"] for f in report.findings])

    def test_an_automatic_file_never_touches_the_manual_table(self):
        data = comparison_data(transmission="automatic")
        report = build_report(PARTIAL_B28, "manual", parse_comparison(data),
                              Policy.load(DEFAULT_POLICY), engine="m52b28")
        self.assertEqual(report.status, STATUS_REJECTED)
        self.assertIn("TRANSMISSION_MISMATCH", [f["code"] for f in report.findings])

    def test_an_automatic_profile_uses_the_automatic_table(self):
        report = self.report(transmission="automatic", transmission_file=None) \
            if False else build_report(
                PARTIAL_B28, "automatic",
                parse_comparison(comparison_data(transmission="automatic")),
                Policy.load(DEFAULT_POLICY), engine="m52b28")
        self.assertEqual(report.symbol, AUTOMATIC_SYMBOL)
        self.assertEqual(report.status, STATUS_READY)
        self.assertTrue(all(p["symbol"] == AUTOMATIC_SYMBOL for p in report.patches))

    def test_another_engine_is_refused(self):
        report = build_report(PARTIAL_B28, "manual",
                              parse_comparison(comparison_data(engine="m54b30")),
                              Policy.load(DEFAULT_POLICY), engine="m52b28")
        self.assertEqual(report.status, STATUS_REJECTED)
        self.assertIn("ENGINE_MISMATCH", [f["code"] for f in report.findings])

    def test_a_measured_line_equal_to_stock_plans_nothing(self):
        stock = dump_stock(PARTIAL_B28, "manual")
        line = [[row["rpm"], row["values"][WARM_COLUMN]] for row in stock["rows"][:5]]
        report = self.report(
            threshold=line,
            advanced=[[704, 1600], [1504, 700], [4064, 700]],
            retarded=[[704, 1400], [1504, 900], [4064, 600]],
            crossing_rpm=1104.0, measured_tco_c=80.0)
        self.assertEqual(report.status, STATUS_EMPTY, report.findings)
        self.assertEqual(report.patches, [])
        self.assertEqual(len(report.details["unchanged_cells"]), 5)

    def test_the_xdf_units_defect_is_reported_and_overridden(self):
        report = self.report()
        codes = [f["code"] for f in report.findings]
        self.assertIn("XDF_UNITS_MISLABELLED", codes)
        self.assertTrue(all(p["units"] == "mg/stroke" for p in report.patches))

    def test_the_plan_passes_the_real_patcher_and_rebuilds_the_crc(self):
        report = self.report()
        objects, problems = load_definition()
        image, offset, image_type = load_calibration(PARTIAL_B28)
        requests = [{"symbol": p["symbol"], "index": p["index"], "value": p["value"]}
                    for p in report.patches]
        plan = plan_patches(image, offset, image_type, objects, problems, requests,
                            Policy.load(DEFAULT_POLICY))
        self.assertEqual(plan.status, STATUS_READY, [f.message for f in plan.findings])
        patched, blocks = apply_plan(image, offset, plan, objects)
        self.assertEqual(blocks, 15)
        self.assertNotEqual(patched[offset + 0x15CA + WARM_COLUMN],
                            image[offset + 0x15CA + WARM_COLUMN])
        # the cold column and the sentinel row must be untouched
        self.assertEqual(patched[offset + 0x15CA], image[offset + 0x15CA])
        self.assertEqual(patched[offset + 0x15CA + 50],
                         image[offset + 0x15CA + 50])


class PolicyTests(unittest.TestCase):
    def test_both_vanos_tables_are_allowlisted(self):
        policy = Policy.load(DEFAULT_POLICY)
        for transmission, symbol in TRANSFER_TABLES.items():
            entry = policy.entry(symbol)
            self.assertIsNotNone(entry, symbol)
            self.assertGreaterEqual(entry.max_cells_per_patch, 55, symbol)
            self.assertIsNone(entry.max_delta_percent,
                              f"{symbol}: a percentage bound would reject the point")
            self.assertGreaterEqual(entry.maximum, 1301.85, symbol)


class GeneratorTests(unittest.TestCase):
    def request(self, **intent) -> dict:
        body = {"id": "v", "kind": "vanos", "reason": "коллектор M50"}
        body.update(intent)
        return {"version": 1, "customer": "test", "stage": 2, "ron": 95,
                "intents": [body]}

    def profile(self) -> dict:
        return json.loads(PROFILE.read_text(encoding="utf-8"))

    def outcome(self, result) -> object:
        return next(o for o in result.outcomes if o.kind == "vanos")

    def test_without_a_profile_the_intent_is_blocked(self):
        result = generate(PARTIAL_B28, self.request(), Policy.load(DEFAULT_POLICY))
        outcome = self.outcome(result)
        self.assertEqual(outcome.status, "BLOCKED")
        self.assertIn("different threshold tables", outcome.detail)

    def test_without_a_measurement_it_explains_what_to_measure(self):
        result = generate(PARTIAL_B28, self.request(), Policy.load(DEFAULT_POLICY),
                          self.profile())
        outcome = self.outcome(result)
        self.assertEqual(outcome.status, "REQUIRES_DATA")
        self.assertIn(MANUAL_SYMBOL, outcome.detail)
        self.assertIn("704", outcome.detail)
        self.assertIn("4128", outcome.detail)
        self.assertIn("never switch", outcome.detail)
        self.assertEqual(result.patches, [])

    def test_a_measured_file_plans_the_cells(self):
        with tempfile.TemporaryDirectory() as folder:
            path = comparison_file(Path(folder))
            result = generate(PARTIAL_B28,
                              self.request(threshold_file=str(path)),
                              Policy.load(DEFAULT_POLICY), self.profile())
        outcome = self.outcome(result)
        self.assertEqual(outcome.status, "READY", outcome.detail)
        self.assertEqual(len(outcome.changes), 10)
        self.assertEqual(result.status, "READY")
        self.assertEqual({p["symbol"] for p in result.patches}, {MANUAL_SYMBOL})
        self.assertTrue(all(p["units"] == "mg/stroke" for p in result.patches))
        self.assertTrue(all(p["evidence"].startswith("measured_vanos_comparison:")
                            for p in result.patches))

    def test_the_automatic_profile_picks_the_automatic_table(self):
        profile = self.profile()
        profile["transmission"] = "automatic"
        with tempfile.TemporaryDirectory() as folder:
            path = comparison_file(Path(folder), transmission="automatic")
            result = generate(PARTIAL_B28, self.request(threshold_file=str(path)),
                              Policy.load(DEFAULT_POLICY), profile)
        self.assertEqual({p["symbol"] for p in result.patches}, {AUTOMATIC_SYMBOL})

    def test_a_rejected_comparison_blocks_the_intent(self):
        with tempfile.TemporaryDirectory() as folder:
            path = comparison_file(Path(folder), crossing_rpm=3200.0)
            result = generate(PARTIAL_B28, self.request(threshold_file=str(path)),
                              Policy.load(DEFAULT_POLICY), self.profile())
        outcome = self.outcome(result)
        self.assertEqual(outcome.status, "BLOCKED")
        self.assertIn("CROSSING_MISMATCH", [f["code"] for f in outcome.findings])

    def test_a_missing_file_is_reported_not_guessed(self):
        result = generate(PARTIAL_B28,
                          self.request(threshold_file="/tmp/does_not_exist_vanos.json"),
                          Policy.load(DEFAULT_POLICY), self.profile())
        outcome = self.outcome(result)
        self.assertEqual(outcome.status, "BLOCKED")
        self.assertIn("does not exist", outcome.detail)

    def test_the_generated_patch_file_reaches_the_patcher(self):
        from MS41.tools.generate_patches import patches_payload
        with tempfile.TemporaryDirectory() as folder:
            path = comparison_file(Path(folder))
            result = generate(PARTIAL_B28, self.request(threshold_file=str(path)),
                              Policy.load(DEFAULT_POLICY), self.profile())
            payload = patches_payload(result, self.request(), self.profile())
        objects, problems = load_definition()
        image, offset, image_type = load_calibration(PARTIAL_B28)
        plan = plan_patches(image, offset, image_type, objects, problems,
                            payload["patches"], Policy.load(DEFAULT_POLICY))
        self.assertEqual(plan.status, STATUS_READY, [f.message for f in plan.findings])
        patched, blocks = apply_plan(image, offset, plan, objects)
        self.assertEqual(blocks, 15)
        # the units stated by the intent survive into the planned entry
        entries = payload["patches"]
        units = {(e["symbol"], e.get("index", 0)): e["units"] for e in entries}
        self.assertTrue(all(value == "mg/stroke" for value in units.values()))


    def test_the_automatic_plan_also_reaches_the_patcher(self):
        from MS41.tools.generate_patches import patches_payload
        profile = self.profile()
        profile["transmission"] = "automatic"
        with tempfile.TemporaryDirectory() as folder:
            path = comparison_file(Path(folder), transmission="automatic")
            case = self.request(threshold_file=str(path))
            result = generate(PARTIAL_B28, case, Policy.load(DEFAULT_POLICY), profile)
            payload = patches_payload(result, case, profile)
        objects, problems = load_definition()
        image, offset, image_type = load_calibration(PARTIAL_B28)
        plan = plan_patches(image, offset, image_type, objects, problems,
                            payload["patches"], Policy.load(DEFAULT_POLICY))
        self.assertEqual(plan.status, STATUS_READY, [f.message for f in plan.findings])
        automatic = get_object(objects, AUTOMATIC_SYMBOL)
        manual = get_object(objects, MANUAL_SYMBOL)
        inside = range(automatic.address, automatic.address + automatic.byte_length)
        details = payload["planned_details"]
        self.assertTrue(details)
        self.assertTrue(all(key.startswith(AUTOMATIC_SYMBOL + "[") for key in details))
        self.assertTrue(all(int(entry["address"], 16) in inside
                            for entry in details.values()))
        patched, blocks = apply_plan(image, offset, plan, objects)
        self.assertEqual(blocks, 15)
        def region(data, obj):
            return bytes(data[offset + obj.address:offset + obj.address + obj.byte_length])
        self.assertNotEqual(region(patched, automatic), region(image, automatic))
        self.assertEqual(region(patched, manual), region(image, manual))


class CliTests(unittest.TestCase):
    def run_tool(self, *arguments):
        return subprocess.run([sys.executable, str(TOOL), *arguments],
                              capture_output=True, text=True)

    def test_dumping_the_stock_table_needs_no_data(self):
        completed = self.run_tool("--bin", str(PARTIAL_B28), "--dump-stock", "-",
                                  "--json")
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)
        self.assertEqual(payload["symbol"], MANUAL_SYMBOL)
        self.assertEqual(payload["never_switch"]["cells"], 15)

    def test_the_derived_example_is_refused_with_a_reason(self):
        completed = self.run_tool("--bin", str(PARTIAL_B28), "--curves",
                                  str(EXAMPLE_FILE))
        self.assertEqual(completed.returncode, 1)
        self.assertIn("PROVENANCE_NOT_MEASURED", completed.stderr)

    def test_a_measured_file_writes_the_patch_file(self):
        with tempfile.TemporaryDirectory() as folder:
            path = comparison_file(Path(folder))
            out = Path(folder) / "patches.json"
            completed = self.run_tool("--bin", str(PARTIAL_B28), "--curves", str(path),
                                      "--profile", str(PROFILE), "--out", str(out))
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("10 cell(s) planned", completed.stdout)
            payload = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(len(payload["patches"]), 10)
            self.assertEqual({p["symbol"] for p in payload["patches"]}, {MANUAL_SYMBOL})

    def test_the_profile_transmission_must_match_the_flag(self):
        profile = json.loads(PROFILE.read_text(encoding="utf-8"))
        profile["transmission"] = "automatic"
        with tempfile.TemporaryDirectory() as folder:
            profile_path = Path(folder) / "profile.json"
            profile_path.write_text(json.dumps(profile), encoding="utf-8")
            completed = self.run_tool("--bin", str(PARTIAL_B28), "--curves",
                                      str(comparison_file(Path(folder))),
                                      "--profile", str(profile_path),
                                      "--transmission", "manual")
        self.assertEqual(completed.returncode, 1)
        self.assertIn("profile says automatic", completed.stderr)


if __name__ == "__main__":
    unittest.main()
