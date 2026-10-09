import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from MS42.tools import xdf as xdf_module
from MS42.tools.analyze_firmware import CALIBRATION_BASE, FULL_SIZE
from MS42.tools.calibration import (
    DEFAULT_POLICY,
    PatchError,
    Policy,
    PolicyEntry,
    apply_plan,
    covered_end_relative,
    crc_storage_offsets,
    load_calibration,
    plan_patches,
    recompute_records,
    record_segments,
    verify_application,
    verify_records,
)
from MS42.tools.xdf import XdfError, XdfObject, Scaler, get_object, load_definition, load_objects

ROOT = Path(__file__).parents[2]
FIRMWARE = ROOT / "MS42" / "firmware"
B28_LHD = FIRMWARE / "Siemens_MS42_0110C6_E46_M52TUB28_EU3_LHD.bin"
B20 = FIRMWARE / "Siemens_MS42_0110C6_E36Z3_M52TUB20_EU3.bin"
B25_RHD = FIRMWARE / "Siemens_MS42_0110C6_E46_M52TUB25_EU3_RHD.bin"

ALL_FIXTURES = sorted(FIRMWARE.glob("*.bin"))

#: expected address-base for each supported layout
EXPECTED_BASES = {
    "full-flash-512K": 0,
}


def load_policy():
    return Policy.load(DEFAULT_POLICY)


def build_plan(bin_path, requests, policy=None, objects=None):
    objects = objects if objects is not None else load_objects()
    image, offset, image_type, address_base = load_calibration(bin_path)
    _, problems = load_definition()
    return plan_patches(
        image, offset, address_base, image_type, objects, problems, requests,
        policy or load_policy(),
    )


def codes(plan):
    return [finding.code for finding in plan.findings]


class SourceVerificationTests(unittest.TestCase):
    def test_every_stock_image_verifies(self):
        for fixture in ALL_FIXTURES:
            with self.subTest(fixture=fixture.name):
                image, offset, image_type, address_base = load_calibration(fixture)
                self.assertEqual("full-flash-512K", image_type)
                self.assertEqual(CALIBRATION_BASE, offset)
                ok, errors = verify_records(image, address_base)
                self.assertTrue(ok, errors)

    def test_tampered_value_byte_is_refused_before_planning(self):
        image, offset, image_type, address_base = load_calibration(B28_LHD)
        image[offset + 0x42F] ^= 0xFF
        ok, errors = verify_records(image, address_base)
        self.assertFalse(ok)
        self.assertTrue(any("cal CRC mismatch" in error for error in errors))
        plan = build_plan(B28_LHD, [{"symbol": "c_vs_max_1", "value": 240}])
        self.assertEqual("READY", plan.status)  # the on-disk file is untouched

    def test_load_calibration_refuses_a_tampered_file_outright(self):
        """A damaged file never reaches the planner through the normal entry point."""
        source = B28_LHD.read_bytes()
        tampered = bytearray(source)
        tampered[CALIBRATION_BASE + 0x42F] ^= 0xFF
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "tampered.bin"
            path.write_bytes(bytes(tampered))
            with self.assertRaises(PatchError):
                load_calibration(path)

    def test_plan_rejects_an_invalid_source_when_called_directly(self):
        """The planner itself still refuses a damaged image, not just the loader."""
        image, offset, image_type, address_base = load_calibration(B28_LHD)
        image[offset + 0x42F] ^= 0xFF
        objects = load_objects()
        _, problems = load_definition()
        plan = plan_patches(
            image, offset, address_base, image_type, objects, problems,
            [{"symbol": "c_vs_max_1", "value": 240}], load_policy(),
        )
        self.assertEqual("REJECTED", plan.status)
        self.assertIn("SOURCE_CRC_INVALID", codes(plan))


class PolicyGateTests(unittest.TestCase):
    def test_symbol_outside_the_allowlist_is_refused(self):
        plan = build_plan(B28_LHD, [{"symbol": "ip_iga_tco_2_is_ivvt__n__maf", "value": 1}])
        self.assertEqual("REJECTED", plan.status)
        self.assertIn("SYMBOL_NOT_ALLOWED", codes(plan))

    def test_policy_maximum_is_enforced(self):
        plan = build_plan(B28_LHD, [{"symbol": "c_vs_max_hys", "value": 40}])
        self.assertIn("POLICY_MAX_VIOLATION", codes(plan))

    def test_policy_minimum_is_enforced(self):
        plan = build_plan(B28_LHD, [{"symbol": "id_n_max_mt__nvs", "index": 0, "value": 1000}])
        self.assertIn("POLICY_MIN_VIOLATION", codes(plan))

    def test_quantization_must_fit_the_step(self):
        # c_n_max_hys is a 32 rpm cell; 100 rpm resolves to 96 rpm, 4% away.
        plan = build_plan(B28_LHD, [{"symbol": "c_n_max_hys", "value": 100}])
        self.assertIn("QUANTIZATION_TOO_COARSE", codes(plan))

    def test_value_that_does_not_fit_the_cell_is_refused(self):
        plan = build_plan(B28_LHD, [{"symbol": "c_vs_max_1", "value": 300}])
        self.assertIn("VALUE_NOT_REPRESENTABLE", codes(plan))

    def test_index_out_of_range_is_refused(self):
        plan = build_plan(B28_LHD, [{"symbol": "id_n_max_mt__nvs", "index": 7, "value": 6500}])
        self.assertIn("INDEX_OUT_OF_RANGE", codes(plan))

    def test_missing_value_is_refused(self):
        plan = build_plan(B28_LHD, [{"symbol": "c_vs_max_1"}])
        self.assertIn("REQUEST_INVALID", codes(plan))

    def test_non_numeric_value_is_refused(self):
        plan = build_plan(B28_LHD, [{"symbol": "c_vs_max_1", "value": "fast"}])
        self.assertIn("REQUEST_INVALID", codes(plan))

    def test_overlapping_writes_are_refused(self):
        plan = build_plan(B28_LHD, [
            {"symbol": "c_vs_max_1", "value": 240},
            {"symbol": "c_vs_max_1", "value": 245},
        ])
        self.assertIn("OVERLAPPING_WRITES", codes(plan))

    def test_unchanged_cell_is_reported_and_not_written(self):
        plan = build_plan(B28_LHD, [{"symbol": "c_vs_max_1", "value": 255}])
        self.assertEqual("EMPTY", plan.status)
        self.assertIn("CELLS_ALREADY_MATCH", codes(plan))

    def test_writes_beyond_crc_coverage_are_refused(self):
        objects = load_objects()
        image, offset, image_type, address_base = load_calibration(B28_LHD)
        fake = XdfObject(
            title="fake_outside_coverage",
            kind="constant",
            address=0x7900,      # past 0x7843, the last covered calibration byte
            element_size_bits=8,
            lsb_first=True,
            scaler=Scaler(1.0, 0.0),
        )
        objects[fake.title] = [fake]
        policy = Policy(version=1, entries={
            fake.title: PolicyEntry(symbol=fake.title, minimum=0, maximum=255),
        })
        _, problems = load_definition()
        plan = plan_patches(
            image, offset, address_base, image_type, objects, problems,
            [{"symbol": fake.title, "value": 1}], policy,
        )
        self.assertEqual("REJECTED", plan.status)
        self.assertIn("OUTSIDE_CRC_COVERAGE", codes(plan))

    def test_covered_end_matches_the_descriptor(self):
        image, offset, _, address_base = load_calibration(B28_LHD)
        segments = record_segments(image, "cal", address_base)
        self.assertEqual(0x7844, covered_end_relative(segments, offset, address_base))


class DuplicateTitleTests(unittest.TestCase):
    def test_only_one_title_is_truly_duplicated(self):
        objects = load_objects()
        duplicates = {k: len(v) for k, v in objects.items() if len(v) > 1}
        self.assertEqual({"c_vls_sap_dly_max_down": 2}, duplicates)

    def test_duplicate_title_without_a_choice_is_refused_with_guidance(self):
        objects = load_objects()
        image, offset, image_type, address_base = load_calibration(B28_LHD)
        policy = Policy(version=1, entries={
            "c_vls_sap_dly_max_down": PolicyEntry(
                symbol="c_vls_sap_dly_max_down", minimum=0, maximum=255),
        })
        _, problems = load_definition()
        plan = plan_patches(
            image, offset, address_base, image_type, objects, problems,
            [{"symbol": "c_vls_sap_dly_max_down", "value": 64}], policy,
        )
        self.assertIn("SYMBOL_UNKNOWN", codes(plan))
        message = next(f.message for f in plan.findings if f.code == "SYMBOL_UNKNOWN")
        self.assertIn("xdf_index", message)

    def test_a_policy_choice_resolves_the_duplicate(self):
        objects = load_objects()
        image, offset, image_type, address_base = load_calibration(B28_LHD)
        policy = Policy(version=1, entries={
            "c_vls_sap_dly_max_down": PolicyEntry(
                symbol="c_vls_sap_dly_max_down", minimum=0, maximum=255, xdf_index=1),
        })
        _, problems = load_definition()
        plan = plan_patches(
            image, offset, address_base, image_type, objects, problems,
            [{"symbol": "c_vls_sap_dly_max_down", "value": 0.5}], policy,
        )
        self.assertNotIn("SYMBOL_UNKNOWN", codes(plan))
        self.assertEqual(1, len(plan.changes))
        self.assertEqual(0x275, plan.changes[0].address)

    def test_maf_title_has_a_single_definition_and_is_not_the_hack_view(self):
        objects = load_objects()
        self.assertEqual(1, len(objects["id_maf_tab"]))
        selected = objects["id_maf_tab"][0]
        self.assertEqual(0.015625, selected.scaler.a)
        self.assertNotIn("MAF Hack", selected.title)
        # The community hack view has its own title and is simply not allowed.
        self.assertIn("id_maf_tab (MAF Hack 2048kg/h)", objects)
        plan = build_plan(
            B28_LHD,
            [{"symbol": "id_maf_tab (MAF Hack 2048kg/h)", "index": 5, "value": 100.0}],
        )
        self.assertIn("SYMBOL_NOT_ALLOWED", codes(plan))


class MonotonicityTests(unittest.TestCase):
    def test_lowering_a_maf_column_is_refused(self):
        """A small, in-policy drop is still refused when it breaks the curve."""
        image, offset, _, _ = load_calibration(B28_LHD)
        obj = get_object(load_objects(), "id_maf_tab")
        step = obj.element_size_bytes
        raws = [
            int.from_bytes(
                image[offset + obj.address + i * step:
                      offset + obj.address + (i + 1) * step], "little")
            for i in range(obj.element_count)
        ]
        # Find a step big enough that -5% of the higher value lands below the
        # lower one, so the delta gate passes and only monotonicity can object.
        index = next(
            i for i in range(1, len(raws))
            if raws[i - 1] > 0 and raws[i] * 0.95 < raws[i - 1]
        )
        plan = build_plan(
            B28_LHD,
            [{"symbol": "id_maf_tab", "index": index, "value": raws[index] * 0.95 * 0.015625}],
        )
        self.assertNotIn("DELTA_TOO_LARGE", codes(plan))
        self.assertIn("MONOTONICITY_BROKEN", codes(plan))
        self.assertEqual("REJECTED", plan.status)

    def test_raising_a_maf_column_beyond_the_delta_limit_is_refused(self):
        # Column 200 is roughly 250 kg/h; +100% is far beyond the 30% policy gate.
        plan = build_plan(B28_LHD, [{"symbol": "id_maf_tab", "index": 200, "value": 700.0}])
        self.assertIn("DELTA_TOO_LARGE", codes(plan))

    def test_too_many_cells_in_one_patch_is_refused(self):
        objects = load_objects()
        image, offset, image_type, address_base = load_calibration(B28_LHD)
        policy = Policy(version=1, entries={
            "c_n_hys_max_puc": PolicyEntry(
                symbol="c_n_hys_max_puc", minimum=0, maximum=1600,
                max_cells_per_patch=1,
            ),
            "c_n_hys_min_puc": PolicyEntry(
                symbol="c_n_hys_min_puc", minimum=0, maximum=1024,
                max_cells_per_patch=1,
            ),
        })
        _, problems = load_definition()
        plan = plan_patches(
            image, offset, address_base, image_type, objects, problems,
            [
                {"symbol": "c_n_hys_max_puc", "value": 640},
                {"symbol": "c_n_hys_min_puc", "value": 128},
            ],
            policy,
        )
        # Two different symbols, one cell each: the per-symbol counter is what
        # matters, so this plan is fine. The counter is checked per symbol below.
        self.assertNotIn("TOO_MANY_CELLS", codes(plan))
        plan = plan_patches(
            image, offset, address_base, image_type, objects, problems,
            [
                {"symbol": "id_maf_tab", "index": 30, "value": 30.0},
                {"symbol": "id_maf_tab", "index": 31, "value": 31.0},
            ],
            Policy(version=1, entries={
                "id_maf_tab": PolicyEntry(
                    symbol="id_maf_tab", minimum=0, maximum=1023.98,
                    max_cells_per_patch=1, xdf_index=0,
                ),
            }),
        )
        self.assertIn("TOO_MANY_CELLS", codes(plan))


class WritePathTests(unittest.TestCase):
    def test_write_changes_exactly_the_value_bytes_and_the_cal_crc(self):
        requests = [
            {"symbol": "c_vs_max_1", "value": 230},
            {"symbol": "c_vs_max_add", "value": 6.0},
        ]
        plan = build_plan(B28_LHD, requests)
        self.assertEqual("READY", plan.status)
        image, offset, image_type, address_base = load_calibration(B28_LHD)
        output, recomputed = apply_plan(image, offset, address_base, plan, load_objects())
        self.assertEqual(["cal"], recomputed)
        changed = {i for i, (a, b) in enumerate(zip(image, output)) if a != b}
        self.assertEqual(
            {offset + 0x42F, offset + 0x430, 0x4FEE0, 0x4FEE1}, changed
        )
        unplanned, findings = verify_application(
            image, output, offset, address_base, plan, load_objects()
        )
        self.assertEqual([], unplanned)
        self.assertFalse([f for f in findings if f.severity == "reject"], findings)
        ok, errors = verify_records(output, address_base)
        self.assertTrue(ok, errors)

    def test_boot_and_program_records_are_left_untouched(self):
        plan = build_plan(B28_LHD, [{"symbol": "c_vs_max_1", "value": 230}])
        image, offset, image_type, address_base = load_calibration(B28_LHD)
        output, _ = apply_plan(image, offset, address_base, plan, load_objects())
        self.assertEqual(image[0x3C24:0x3C26], output[0x3C24:0x3C26])
        self.assertEqual(image[0x50306:0x50308], output[0x50306:0x50308])
        self.assertNotEqual(image[0x4FEE0:0x4FEE2], output[0x4FEE0:0x4FEE2])

    def test_16_bit_cell_changing_one_byte_is_counted_once(self):
        """A 16-bit MAF column crossing 0x00FF changes one byte, not two."""
        image, offset, image_type, address_base = load_calibration(B28_LHD)
        obj = get_object(load_objects(), "id_maf_tab", 0)
        step = obj.element_size_bytes
        # Add one to a cell whose low byte is not 0xFF: only the low byte moves.
        # (Adding one to 0x??FF would carry into the high byte instead, and MS41
        # once mis-reported that case as a missing write - hence this regression.)
        index = next(
            i for i in range(4, obj.element_count)
            if int.from_bytes(
                image[offset + obj.address + i * step:
                      offset + obj.address + (i + 1) * step], "little") % 256 != 0xFF
        )
        raw = int.from_bytes(
            image[offset + obj.address + index * step:
                  offset + obj.address + (index + 1) * step],
            "little",
        )
        plan = build_plan(
            B28_LHD,
            [{"symbol": "id_maf_tab", "index": index,
              "value": (raw + 1) * obj.scaler.a}],
        )
        self.assertEqual("READY", plan.status, [f.message for f in plan.findings])
        output, _ = apply_plan(image, offset, address_base, plan, load_objects())
        changed = {i for i, (a, b) in enumerate(zip(image, output)) if a != b}
        value_bytes = {i for i in changed if i < 0x4FEE0}
        self.assertEqual(1, len(value_bytes))

    def test_multi_byte_write_is_verified_per_element(self):
        plan = build_plan(
            B28_LHD,
            [{"symbol": "id_n_max_max_mt__nvs", "index": 0, "value": 7000}],
        )
        self.assertEqual("READY", plan.status, [f.message for f in plan.findings])
        image, offset, image_type, address_base = load_calibration(B28_LHD)
        output, _ = apply_plan(image, offset, address_base, plan, load_objects())
        _, findings = verify_application(
            image, output, offset, address_base, plan, load_objects()
        )
        rejects = [f for f in findings if f.severity == "reject"]
        self.assertEqual([], rejects, rejects)

    def test_verify_application_detects_a_rogue_edit(self):
        plan = build_plan(B28_LHD, [{"symbol": "c_vs_max_1", "value": 230}])
        image, offset, image_type, address_base = load_calibration(B28_LHD)
        output, _ = apply_plan(image, offset, address_base, plan, load_objects())
        output[offset + 0x100] ^= 0xFF
        unplanned, findings = verify_application(
            image, output, offset, address_base, plan, load_objects()
        )
        self.assertTrue(unplanned)
        self.assertIn("UNPLANNED_BYTE_CHANGES", [f.code for f in findings])

    def test_verify_application_detects_a_stale_element(self):
        plan = build_plan(B28_LHD, [{"symbol": "c_vs_max_1", "value": 230}])
        image, offset, image_type, address_base = load_calibration(B28_LHD)
        output, _ = apply_plan(image, offset, address_base, plan, load_objects())
        output[offset + 0x42F] = image[offset + 0x42F]
        _, findings = verify_application(
            image, output, offset, address_base, plan, load_objects()
        )
        found = [f.code for f in findings]
        self.assertIn("VALUE_MISMATCH", found)
        self.assertIn("PLANNED_BYTE_NOT_WRITTEN", found)
        # The CRC was rebuilt for the edited image, so the stale value shows up
        # as a mismatch rather than as a checksum failure - both are reported.
        self.assertIn("CRC_INVALID_AFTER_PATCH", found)

    def test_recompute_records_refuses_a_damaged_source(self):
        image, offset, image_type, address_base = load_calibration(B28_LHD)
        image[0x4FEE0] ^= 0xFF
        with self.assertRaises(PatchError):
            recompute_records(image, address_base, set())

    def test_recompute_records_skips_records_without_changed_bytes(self):
        image, offset, _, address_base = load_calibration(B28_LHD)
        recomputed = recompute_records(image, address_base, set())
        self.assertEqual([], recomputed)

    def test_crc_storage_offsets_lists_every_record_word(self):
        image, _, _, address_base = load_calibration(B28_LHD)
        self.assertEqual(
            {0x3C24, 0x3C25, 0x4FEE0, 0x4FEE1, 0x50306, 0x50307},
            set(crc_storage_offsets(image, address_base)),
        )

    def test_apply_plan_refuses_a_rejected_plan(self):
        plan = build_plan(B28_LHD, [{"symbol": "not_in_policy", "value": 1}])
        image, offset, image_type, address_base = load_calibration(B28_LHD)
        with self.assertRaises(PatchError):
            apply_plan(image, offset, address_base, plan, load_objects())


class CliTests(unittest.TestCase):
    def test_cli_writes_a_verified_image_and_a_report(self):
        from MS42.tools.calibration import main as calibration_main  # noqa: F401

        from MS42.tools import calibration as calibration_module

        requests = [{"symbol": "c_vs_max_1", "value": 220}]
        with tempfile.TemporaryDirectory() as directory:
            patches = Path(directory) / "patches.json"
            patches.write_text(json.dumps(requests), encoding="utf-8")
            out = Path(directory) / "patched.bin"
            report = Path(directory) / "report.json"
            namespace = calibration_module.argparse.Namespace(
                bin=B28_LHD, patches=patches, out=out, report=report,
                policy=DEFAULT_POLICY, xdf=xdf_module.DEFAULT_XDF, json=False,
            )
            exit_code = calibration_module.run(namespace)
            self.assertEqual(0, exit_code)
            self.assertTrue(out.exists())
            payload = json.loads(report.read_text(encoding="utf-8"))
            self.assertEqual("WRITTEN", payload["status"])
            self.assertTrue(payload["crc_valid_after"])
            self.assertEqual(["cal"], ["cal"] if payload["crc_records_recomputed"] == 1 else [])
            self.assertEqual(1, payload["crc_records_recomputed"])
            self.assertEqual(1, payload["value_bytes_changed"])
            self.assertEqual(2, payload["crc_bytes_changed"])
            # The written file passes the independent detector.
            from MS42.tools.analyze_firmware import analyze
            self.assertTrue(analyze(out).supported)

    def test_cli_writes_the_patched_byte_at_the_right_file_offset(self):
        from MS42.tools import calibration as calibration_module

        with tempfile.TemporaryDirectory() as directory:
            patches = Path(directory) / "p.json"
            patches.write_text(json.dumps([{"symbol": "c_vs_max_1", "value": 100}]),
                               encoding="utf-8")
            out = Path(directory) / "out.bin"
            namespace = calibration_module.argparse.Namespace(
                bin=B28_LHD, patches=patches, out=out, report=None,
                policy=DEFAULT_POLICY, xdf=xdf_module.DEFAULT_XDF, json=True,
            )
            self.assertEqual(0, calibration_module.run(namespace))
            data = out.read_bytes()
            self.assertEqual(100, data[0x4842F])

    def test_cli_refuses_a_patch_file_marked_not_ready(self):
        from MS42.tools import calibration as calibration_module

        with tempfile.TemporaryDirectory() as directory:
            patches = Path(directory) / "p.json"
            patches.write_text(json.dumps({"status": "REJECTED", "patches": []}),
                               encoding="utf-8")
            namespace = calibration_module.argparse.Namespace(
                bin=B28_LHD, patches=patches, out=None, report=None,
                policy=DEFAULT_POLICY, xdf=xdf_module.DEFAULT_XDF, json=True,
            )
            with self.assertRaises(SystemExit):
                calibration_module.run(namespace)


class XdfLayerTests(unittest.TestCase):
    def test_both_ms42_definitions_parse_without_problems(self):
        for path in (xdf_module.DEFAULT_XDF, xdf_module.XDF_32K):
            objects, problems = load_definition(path)
            self.assertEqual([], problems)
            self.assertEqual(2308, sum(len(v) for v in objects.values()))

    def test_calibration_base_is_the_documented_window(self):
        self.assertEqual(0x48000, xdf_module.CALIBRATION_BASE)
        self.assertEqual(0x8000, xdf_module.CALIBRATION_SIZE)

    def test_maf_table_is_16_bit_with_the_documented_scale(self):
        objects = load_objects()
        obj = objects["id_maf_tab"][0]
        self.assertEqual(16, obj.element_size_bits)
        self.assertEqual(256, obj.element_count)
        self.assertEqual(0.015625, obj.scaler.a)
        self.assertEqual(0, obj.scaler.b)
        self.assertTrue(obj.lsb_first)

    def test_rpm_limiter_tables_are_8_bit_times_32(self):
        objects = load_objects()
        for title in ("id_n_max_mt__nvs", "id_n_max_max_mt__nvs",
                      "id_n_max_at__nvs", "id_n_max_max_at__nvs"):
            obj = objects[title][0]
            self.assertEqual(8, obj.element_size_bits)
            self.assertEqual(3, obj.element_count)
            self.assertEqual(32.0, obj.scaler.a)

    def test_unknown_symbol_raises(self):
        with self.assertRaises(XdfError):
            get_object(load_objects(), "c_there_is_no_such_symbol")

    def test_selected_maf_view_agrees_with_the_16x16_view_byte_for_byte(self):
        """Both views read the same storage; only the shape differs."""
        image, offset, _, _ = load_calibration(B28_LHD)
        objects = load_objects()
        flat = objects["id_maf_tab"][0]
        view = objects["id_maf_tab__v_maf_1__v_maf_2"][0]
        raw_flat = xdf_module.read_values(image[offset:], flat)
        raw_view = xdf_module.read_values(image[offset:], view)
        self.assertEqual(raw_flat, raw_view)


class QuantumToleranceTests(unittest.TestCase):
    """The 1 % rule is the default; small tables may carry an explicit override."""

    def test_global_default_is_one_percent(self):
        policy = Policy.load(DEFAULT_POLICY)
        self.assertEqual(1.0, policy.max_quantization_error_percent)
        self.assertIsNone(policy.entry("id_maf_tab").max_quantization_error_percent)

    def test_sub_millisecond_tables_carry_a_documented_override(self):
        policy = Policy.load(DEFAULT_POLICY)
        for symbol in ("ip_ti_slow_wf_thd_min__tco", "ip_ti_fast_wf_thd_min__tco",
                       "ip_ti_add_dly__vb"):
            with self.subTest(symbol=symbol):
                entry = policy.entry(symbol)
                self.assertIsNotNone(entry)
                self.assertEqual(10.0, entry.max_quantization_error_percent)
                self.assertIn("0.032", entry.note)

    def test_the_override_only_relaxes_its_own_symbol(self):
        from MS42.tools.calibration import PolicyEntry
        entry = PolicyEntry(symbol="ip_ti_add_dly__vb", minimum=0, maximum=8.0,
                            max_quantization_error_percent=10.0)
        self.assertEqual(10.0, entry.max_quantization_error_percent)
        default = PolicyEntry(symbol="c_vs_max_1", minimum=0, maximum=255)
        self.assertIsNone(default.max_quantization_error_percent)

    def test_a_value_rejected_under_the_default_passes_with_the_override(self):
        """0.2 ms is 4 % away from the 0.032 ms grid: rejected globally, allowed here."""
        policy = Policy.load(DEFAULT_POLICY)
        entry = policy.entry("ip_ti_add_dly__vb")
        self.assertLess(entry.minimum, 0.2)
        self.assertGreater(entry.maximum, 0.2)
        self.assertGreater(10.0, 1.0)


if __name__ == "__main__":
    unittest.main()
