import argparse
import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path

from MS41.tools import calibration as calibration_module
from MS41.tools.calibration import (
    DEFAULT_POLICY,
    PatchPlan,
    PlannedChange,
    Policy,
    apply_plan,
    plan_patches,
)
from MS41.tools.maf_curve import (
    MAF_TITLE,
    MAF_TITLE_SECOND_VIEW,
    SCALE_CEILING_KGH,
    Curve,
    CurveError,
    build_report,
    dump_stock,
    load_curve,
    parse_curve,
    resample,
)
from MS41.tools.xdf import get_object, load_definition, load_objects, read_engineering

ROOT = Path(__file__).parents[2]
FIRMWARE = ROOT / "MS41" / "firmware"
PARTIAL_B28 = FIRMWARE / "MS41_1429861_E36_328i_M52B28_1997_4101_partial.bin"
PARTIAL_B20 = FIRMWARE / "MS41_1429861_E36_320i_M52B20_1996_4101_partial.bin"
STOCK_CURVE = ROOT / "MS41" / "examples" / "maf_curve_stock_b28.example.json"
EXAMPLE_CURVE = ROOT / "MS41" / "examples" / "maf_curve_example.json"


def curve(points, **overrides):
    data = {
        "sensor": "test sensor",
        "source": "test fixture",
        "provenance": "datasheet",
        "units": "kg/h",
        "points": points,
    }
    data.update(overrides)
    return data


class ParseTests(unittest.TestCase):
    def test_valid_curve(self):
        parsed = parse_curve(curve([[0.0, 0.0], [2.5, 100.0], [5.0, 400.0]]))
        self.assertEqual(parsed.sensor, "test sensor")
        self.assertEqual(parsed.points[1], (2.5, 100.0))
        self.assertEqual(parsed.ceiling, 400.0)

    def test_provenance_is_mandatory(self):
        for override in ({"sensor": ""}, {"source": ""}, {"provenance": ""},
                         {"provenance": "guessed"}):
            with self.subTest(override=override):
                with self.assertRaises(CurveError) as caught:
                    parse_curve(curve([[0.0, 0.0], [2.5, 100.0], [5.0, 400.0]], **override))
                self.assertIn("PROVENANCE_MISSING", str(caught.exception))

    def test_units_must_match_the_table(self):
        with self.assertRaises(CurveError) as caught:
            parse_curve(curve([[0.0, 0.0], [2.5, 100.0], [5.0, 400.0]], units="g/s"))
        self.assertIn("UNITS_UNSUPPORTED", str(caught.exception))

    def test_short_curve_is_refused(self):
        with self.assertRaises(CurveError) as caught:
            parse_curve(curve([[0.0, 0.0], [5.0, 400.0]]))
        self.assertIn("CURVE_TOO_SHORT", str(caught.exception))

    def test_voltages_must_increase(self):
        with self.assertRaises(CurveError) as caught:
            parse_curve(curve([[0.0, 0.0], [3.0, 100.0], [2.0, 200.0]]))
        self.assertIn("VOLTAGE_NOT_INCREASING", str(caught.exception))

    def test_flow_must_not_decrease(self):
        with self.assertRaises(CurveError) as caught:
            parse_curve(curve([[0.0, 0.0], [2.5, 300.0], [5.0, 200.0]]))
        self.assertIn("FLOW_DECREASING", str(caught.exception))

    def test_negative_flow_is_refused(self):
        with self.assertRaises(CurveError) as caught:
            parse_curve(curve([[0.0, -5.0], [2.5, 100.0], [5.0, 400.0]]))
        self.assertIn("FLOW_NEGATIVE", str(caught.exception))

    def test_flow_above_the_cell_scale_is_refused(self):
        with self.assertRaises(CurveError) as caught:
            parse_curve(curve([[0.0, 0.0], [2.5, 100.0], [5.0, SCALE_CEILING_KGH + 1]]))
        self.assertIn("FLOW_ABOVE_SCALE", str(caught.exception))

    def test_voltage_beyond_the_adc_range_is_refused(self):
        with self.assertRaises(CurveError):
            parse_curve(curve([[0.0, 0.0], [2.5, 100.0], [5.5, 400.0]]))

    def test_interpolation_and_holding(self):
        parsed = parse_curve(curve([[0.5, 10.0], [1.5, 30.0], [4.0, 200.0]]))
        self.assertEqual(parsed.flow_at(1.0), (20.0, False))
        self.assertEqual(parsed.flow_at(0.1), (10.0, True))
        self.assertEqual(parsed.flow_at(4.5), (200.0, True))
        self.assertEqual(parsed.flow_at(4.0), (200.0, False))


class ResampleTests(unittest.TestCase):
    def test_holds_edges_and_reports_how_much(self):
        parsed = parse_curve(curve([[2.0, 50.0], [3.0, 100.0], [4.0, 200.0]]))
        values, held = resample(parsed, (0.0, 2.0, 3.0, 4.0, 5.0))
        self.assertEqual(values, [50.0, 50.0, 100.0, 200.0, 200.0])
        self.assertEqual(held, {"held_below": 1, "held_above": 1})

    def test_curve_on_the_grid_keeps_monotonicity(self):
        grid = get_object(load_objects(), MAF_TITLE).axis_labels("y")
        parsed = load_curve(EXAMPLE_CURVE)
        values, _ = resample(parsed, grid)
        self.assertTrue(all(b >= a for a, b in zip(values, values[1:])))
        self.assertEqual(len(values), 256)

    def test_resampling_the_stock_curve_reproduces_the_stock_table(self):
        # The grid plus linear interpolation must be exact: dumping the stock curve
        # and feeding it back has to be a no-op, or the grid is wrong.
        dumped = dump_stock(PARTIAL_B28)
        parsed = parse_curve(dumped)
        objects = load_objects()
        maf = get_object(objects, MAF_TITLE)
        stock = read_engineering(PARTIAL_B28.read_bytes(), maf)
        values, _ = resample(parsed, maf.axis_labels("y"))
        self.assertEqual(len(values), len(stock))
        self.assertEqual([round(v, 6) for v in values], [round(v, 6) for v in stock])

    def test_dump_stock_lists_every_column(self):
        dumped = dump_stock(PARTIAL_B28)
        self.assertEqual(len(dumped["points"]), 256)
        self.assertEqual(dumped["provenance"], "derived")
        self.assertIn("not a datasheet", dumped["notes"])

    def test_stock_dump_keeps_flat_runs(self):
        # Collapsing equal neighbours would turn the stock steps into ramps.
        dumped = dump_stock(PARTIAL_B28)
        flows = [flow for _, flow in dumped["points"]]
        self.assertGreater(flows.count(0.0), 5)
        self.assertEqual(len(flows), 256)


class ReportTests(unittest.TestCase):
    def test_identity_curve_writes_nothing(self):
        report = build_report(PARTIAL_B28, load_curve(STOCK_CURVE), Policy.load(DEFAULT_POLICY))
        self.assertEqual(report.status, "EMPTY")
        self.assertEqual(report.patches, [])
        self.assertIn("CURVE_ALREADY_MATCHES", {f["code"] for f in report.findings})

    def test_scaled_curve_is_planned_and_reports_the_deviation(self):
        report = build_report(PARTIAL_B28, load_curve(EXAMPLE_CURVE),
                              Policy.load(DEFAULT_POLICY))
        self.assertEqual(report.status, "READY")
        self.assertEqual(report.plan_status, "READY")
        deviation = report.details["deviation_vs_stock"]
        self.assertEqual(deviation["cells_changed"], 154)
        self.assertEqual(deviation["stock_ceiling_kgh"], 725.0938)
        self.assertEqual(deviation["new_ceiling_kgh"], 768.5994)
        self.assertAlmostEqual(deviation["max_relative_percent"], 6.0, places=2)
        self.assertIn("CEILING_ABOVE_STOCK", {f["code"] for f in report.findings})
        self.assertEqual(len(report.patches), 154)
        self.assertTrue(all(p["evidence"] == "documented_curve:derived"
                            for p in report.patches))
        self.assertTrue(all(p["requires_validation"] for p in report.patches))

    def test_grid_details_come_from_the_proven_grid(self):
        report = build_report(PARTIAL_B28, load_curve(STOCK_CURVE),
                              Policy.load(DEFAULT_POLICY))
        self.assertEqual(report.details["columns"], 256)
        self.assertEqual(report.details["grid_full_scale_volts"], 5.018)
        self.assertAlmostEqual(report.details["grid_step_volts"], 5.018 / 255, places=8)
        self.assertLess(report.details["grid_label_max_error_volts"], 0.001)

    def test_the_16x16_view_has_no_grid_and_is_refused(self):
        with self.assertRaises(CurveError) as caught:
            build_report(PARTIAL_B28, load_curve(STOCK_CURVE),
                         Policy.load(DEFAULT_POLICY), symbol=MAF_TITLE_SECOND_VIEW)
        self.assertIn("grid is incomplete", str(caught.exception))

    def test_shrinking_curve_does_not_warn_about_the_ceiling(self):
        dumped = dump_stock(PARTIAL_B28)
        shrunk = dict(dumped)
        shrunk["points"] = [[v, f * 0.9] for v, f in dumped["points"]]
        report = build_report(PARTIAL_B28, parse_curve(shrunk), Policy.load(DEFAULT_POLICY))
        self.assertEqual(report.status, "READY")
        self.assertNotIn("CEILING_ABOVE_STOCK", {f["code"] for f in report.findings})


class PolicyTests(unittest.TestCase):
    def test_maf_views_allow_a_full_curve(self):
        policy = Policy.load(DEFAULT_POLICY)
        for symbol in (MAF_TITLE, MAF_TITLE_SECOND_VIEW):
            entry = policy.entry(symbol)
            self.assertIsNotNone(entry, symbol)
            self.assertEqual(entry.max_cells_per_patch, 256, symbol)
            self.assertTrue(entry.require_monotonic, symbol)
            self.assertEqual(entry.maximum, 1023, symbol)


class EndToEndTests(unittest.TestCase):
    def test_curve_replacement_writes_and_verifies(self):
        report = build_report(PARTIAL_B28, load_curve(EXAMPLE_CURVE),
                              Policy.load(DEFAULT_POLICY))
        with tempfile.TemporaryDirectory() as directory:
            directory = Path(directory)
            patches = directory / "patches.json"
            out = directory / "patched.bin"
            patches.write_text(json.dumps(
                {"version": 1, "status": "READY", "patches": report.patches}),
                encoding="utf-8")
            args = argparse.Namespace(
                bin=PARTIAL_B28, patches=patches, out=out, report=None,
                policy=DEFAULT_POLICY, xdf=calibration_module.DEFAULT_XDF, json=False,
            )
            with contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(calibration_module.run(args), 0)
            self.assertTrue(out.exists())

            objects = load_objects()
            maf = get_object(objects, MAF_TITLE)
            written = read_engineering(out.read_bytes(), maf)
            requested, _ = resample(load_curve(EXAMPLE_CURVE), maf.axis_labels("y"))
            for index, (want, got) in enumerate(zip(requested, written)):
                self.assertLessEqual(abs(want - got), 0.015625,
                                     f"column {index}: asked {want}, got {got}")
            self.assertTrue(all(b >= a for a, b in zip(written, written[1:])))
            calibration = out.read_bytes()
            ok, blocks, errors = calibration_module.verify_crc_chain(calibration)
            self.assertTrue(ok, errors)
            self.assertEqual(blocks, 15)

    def test_whole_curve_plan_is_accepted_by_the_core(self):
        objects, problems = load_definition()
        image, offset, image_type = calibration_module.load_calibration(PARTIAL_B28)
        maf = get_object(objects, MAF_TITLE)
        values, _ = resample(load_curve(EXAMPLE_CURVE), maf.axis_labels("y"))
        requests = [{"symbol": MAF_TITLE, "index": i, "value": v}
                    for i, v in enumerate(values)]
        plan = plan_patches(image, offset, image_type, objects, problems, requests,
                            Policy.load(DEFAULT_POLICY))
        self.assertEqual(plan.status, "READY")
        self.assertEqual(len(plan.changes), 256)

    def _synthetic_pair(self, old_raw, new_raw, write_new=True):
        """Source/output that differ only in one 16-bit element, CRCs consistent."""
        objects = load_objects()
        image, offset, _ = calibration_module.load_calibration(PARTIAL_B28)
        obj = get_object(objects, "ip_tib__n__maf")
        index = 5
        element = offset + obj.address + index * obj.element_size_bytes

        def with_crc(image_bytes):
            calibration = image_bytes[offset:offset + 0x6000]
            calibration_module.recompute_crc(calibration)
            image_bytes[offset:offset + 0x6000] = calibration
            return image_bytes

        source = bytearray(image)
        source[element:element + 2] = old_raw.to_bytes(2, "little")
        source = with_crc(source)
        output = bytearray(source)
        if write_new:
            output[element:element + 2] = new_raw.to_bytes(2, "little")
        output = with_crc(output)

        change = PlannedChange(
            symbol=obj.title, index=index,
            address=obj.address + index * obj.element_size_bytes,
            byte_offsets=list(obj.byte_offsets(index)),
            old_raw=old_raw, new_raw=new_raw,
            old_engineering=obj.to_engineering(old_raw),
            new_engineering=obj.to_engineering(new_raw),
            requested_engineering=obj.to_engineering(new_raw),
            quantization_error_percent=0.0, delta_percent=None,
            units=obj.units, kind=obj.kind,
        )
        plan = PatchPlan(status="READY", source=str(PARTIAL_B28),
                         source_sha256="0" * 64, image_type="partial",
                         calibration_offset=offset, changes=[change])
        return source, output, offset, plan, objects

    def test_sixteen_bit_change_in_one_byte_verifies(self):
        """Regression for a false PLANNED_BYTE_NOT_WRITTEN.

        0x1000 -> 0x1100 keeps the low byte, so the changed-byte set is a subset of
        the planned offsets. The verifier must judge the element value, not count
        bytes; 0x1001 -> 0x1000 is the mirror case.
        """
        for old_raw, new_raw in ((0x1000, 0x1100), (0x1001, 0x1000)):
            with self.subTest(old_raw=hex(old_raw)):
                source, output, offset, plan, objects = self._synthetic_pair(
                    old_raw, new_raw)
                unplanned, findings = calibration_module.verify_application(
                    source, output, offset, plan, objects)
                self.assertEqual(unplanned, [])
                codes = {f.code for f in findings if f.severity == "reject"}
                self.assertEqual(codes, set(), findings)

    def test_a_value_that_was_not_written_is_still_caught(self):
        source, output, offset, plan, objects = self._synthetic_pair(
            0x1000, 0x1100, write_new=False)
        unplanned, findings = calibration_module.verify_application(
            source, output, offset, plan, objects)
        self.assertEqual(unplanned, [])
        codes = {f.code for f in findings}
        self.assertIn("VALUE_MISMATCH", codes)
        self.assertIn("PLANNED_BYTE_NOT_WRITTEN", codes)

    def test_two_views_share_the_same_bytes(self):
        objects = load_objects()
        first = get_object(objects, MAF_TITLE)
        second = get_object(objects, MAF_TITLE_SECOND_VIEW)
        self.assertEqual(first.address, second.address)
        self.assertEqual(first.byte_length, second.byte_length)
        self.assertEqual(
            [offset for offset in first.byte_offsets(37)],
            [offset for offset in second.byte_offsets(37)],
        )

    def test_cli_writes_a_patch_file(self):
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as directory:
            out = Path(directory) / "patches.json"
            report = Path(directory) / "report.json"
            completed = subprocess.run(
                [sys.executable, str(ROOT / "MS41" / "tools" / "maf_curve.py"),
                 "--stock", str(PARTIAL_B28), "--curve", str(EXAMPLE_CURVE),
                 "--out", str(out), "--report", str(report)],
                capture_output=True, text=True,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            payload = json.loads(out.read_text(encoding="utf-8"))
            self.assertEqual(payload["status"], "READY")
            self.assertEqual(len(payload["patches"]), 154)
            self.assertEqual(json.loads(report.read_text(encoding="utf-8"))["status"], "READY")

    def test_cli_refuses_a_curve_without_provenance(self):
        import subprocess
        import sys
        with tempfile.TemporaryDirectory() as directory:
            bad = Path(directory) / "bad.json"
            bad.write_text(json.dumps(curve([[0.0, 0.0], [2.5, 100.0], [5.0, 400.0]],
                                            source="")), encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, str(ROOT / "MS41" / "tools" / "maf_curve.py"),
                 "--stock", str(PARTIAL_B28), "--curve", str(bad)],
                capture_output=True, text=True,
            )
            self.assertEqual(completed.returncode, 1)
            self.assertIn("PROVENANCE_MISSING", completed.stderr)


if __name__ == "__main__":
    unittest.main()
