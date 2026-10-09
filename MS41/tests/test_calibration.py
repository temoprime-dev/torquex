import dataclasses
import json
import tempfile
import unittest
from pathlib import Path

from MS41.tools import xdf as xdf_module
from MS41.tools.analyze_firmware import _crc16
from MS41.tools.calibration import (
    DEFAULT_POLICY,
    PatchError,
    Policy,
    PolicyEntry,
    apply_plan,
    crc_covered_end,
    crc_storage_offsets,
    load_calibration,
    plan_patches,
    recompute_crc,
    verify_application,
    verify_crc_chain,
)
from MS41.tools.xdf import (
    Scaler,
    XdfError,
    get_object,
    load_definition,
    load_objects,
    read_engineering,
)

ROOT = Path(__file__).parents[2]
FIRMWARE = ROOT / "MS41" / "firmware"
PARTIAL_B28 = FIRMWARE / "MS41_1429861_E36_328i_M52B28_1997_4101_partial.bin"
PARTIAL_B20 = FIRMWARE / "MS41_1429861_E36_320i_M52B20_1996_4101_partial.bin"
FULL_B28 = FIRMWARE / "MS41_1429861_E39_528i_M52B28_1995_0060041010000_full.bin"
MAF_TITLE = "id_maf_tab__v_maf_1__v_maf_2 (1x256)"


def load_policy():
    return Policy.load(DEFAULT_POLICY)


def build_plan(bin_path, requests, policy=None, objects=None):
    objects = objects if objects is not None else load_objects()
    image, offset, image_type = load_calibration(bin_path)
    _, problems = load_definition()
    plan = plan_patches(
        image, offset, image_type, objects, problems, requests, policy or load_policy()
    )
    return image, offset, plan, objects


class ScalerTests(unittest.TestCase):
    def test_identity(self):
        scaler = Scaler.from_equation("X")
        self.assertEqual((scaler.a, scaler.b), (1.0, 0.0))

    def test_scale_and_offset(self):
        scaler = Scaler.from_equation("X*0.75-48")
        self.assertAlmostEqual(scaler.a, 0.75)
        self.assertAlmostEqual(scaler.b, -48.0)

    def test_reversed_and_parenthesised_forms(self):
        for equation in ("0.015625*X", "1-(0.0039058823*X)", "x*0.10196",
                         "0.750000 * X+ -48.000000", "X-32768"):
            scaler = Scaler.from_equation(equation)
            self.assertIsInstance(scaler.a, float, equation)

    def test_round_trip(self):
        scaler = Scaler.from_equation("X*0.75-48")
        for value in (-48.0, -20.0, 0.0, 72.0):
            self.assertAlmostEqual(scaler.to_engineering(scaler.to_raw(value)), value)

    def test_non_linear_equation_is_rejected(self):
        with self.assertRaises(XdfError):
            Scaler.from_equation("X*X")

    def test_two_variables_rejected(self):
        with self.assertRaises(XdfError):
            Scaler.from_equation("X+Y")


class XdfLoadingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.objects = load_objects()

    def test_every_object_is_representable(self):
        _, problems = load_definition()
        self.assertEqual(problems, [])

    def test_object_count_and_address_bounds(self):
        self.assertEqual(sum(len(v) for v in self.objects.values()), 857)
        highest = max(o.address + o.byte_length for v in self.objects.values() for o in v)
        self.assertLessEqual(highest, 0x2D4C, "objects must fit the CRC-covered region")

    def test_16_bit_objects_are_little_endian(self):
        for obj in self.objects["c_iga_lgrd_1"]:
            self.assertEqual(obj.element_size_bits, 16)
            self.assertTrue(obj.lsb_first)

    def test_one_multiplier_reads_as_exactly_one(self):
        # c_cur_igc_fac_max holds 1.0 as raw 32768 with a 1/32768 scale; only the
        # little-endian reading produces a sane value here.
        data = PARTIAL_B28.read_bytes()
        obj = get_object(self.objects, "c_cur_igc_fac_max")
        self.assertAlmostEqual(read_engineering(data, obj)[0], 1.0, places=6)

    def test_known_constants_have_expected_stock_values(self):
        data = PARTIAL_B28.read_bytes()
        expected = {
            "c_n_max": 6496.0,
            "c_n_max_hys": 32.0,
            "c_n_max_hys_max": 320.0,
            "c_vs_max": 255.0,
            "c_tco_min_pu": 72.0,
        }
        for name, value in expected.items():
            obj = get_object(self.objects, name)
            self.assertAlmostEqual(read_engineering(data, obj)[0], value, places=3, msg=name)

    def test_maf_transfer_curve_is_monotonic_in_every_fixture(self):
        for path in sorted(FIRMWARE.glob("*.bin")):
            data = path.read_bytes()
            base = 0 if len(data) == 0x6000 else 0x14000
            obj = get_object(self.objects, MAF_TITLE)
            values = [
                obj.to_engineering(
                    int.from_bytes(
                        data[base + obj.address + i * 2:base + obj.address + i * 2 + 2],
                        "little",
                    )
                )
                for i in range(obj.element_count)
            ]
            self.assertTrue(
                all(b >= a for a, b in zip(values, values[1:])),
                f"{path.name}: MAF curve is not monotonic",
            )

    def test_table_data_address_differs_from_header(self):
        obj = get_object(self.objects, MAF_TITLE)
        self.assertEqual(obj.address, 0x2B4A)
        self.assertEqual(obj.element_count, 256)
        self.assertEqual(obj.byte_length, 512)
        self.assertEqual(obj.address + obj.byte_length, 0x2D4A)

    def test_unknown_symbol_raises(self):
        with self.assertRaises(XdfError):
            get_object(self.objects, "not_a_real_symbol")


class MafGridTests(unittest.TestCase):
    """The MAF speed-density table is the one table whose grid is fully known.

    Its y axis is a static XDF label list (0 ... 5.018 V, 256 points), which makes
    the lookup grid explicit: column i is the air mass the sensor reports at
    i * 5.018 / 255 volts. Axes that point into the binary instead (linked_axes)
    are linkage and must never be read as values.
    """

    @classmethod
    def setUpClass(cls):
        cls.objects = load_objects()
        cls.maf = get_object(cls.objects, MAF_TITLE)

    def test_voltage_grid_is_linear_over_256_points(self):
        grid = self.maf.axis_labels("y")
        self.assertEqual(len(grid), 256)
        self.assertEqual(grid[0], 0.0)
        self.assertEqual(grid[-1], 5.018)
        self.assertTrue(all(b > a for a, b in zip(grid, grid[1:])))
        step = 5.018 / 255
        self.assertLess(max(abs(grid[i] - step * i) for i in range(256)), 0.001,
                        "labels are rounded to 1 mV; the grid itself is linear")
        self.assertAlmostEqual(step, 0.0196784, places=6)

    def test_x_axis_is_a_placeholder_for_this_table(self):
        self.assertEqual(self.maf.axis_labels("x"), (0.0,))
        self.assertEqual(self.maf.linked_axes, ())

    def test_linked_axes_are_reported_as_linkage_not_as_grids(self):
        linked = get_object(self.objects, "id_maf_hys_ivvt__n")
        self.assertIn("y", linked.linked_axes)
        self.assertEqual(linked.axis_labels("y"), ())
        # Its x axis carries a single static label, which is not a grid either.
        self.assertLessEqual(len(linked.axis_labels("x")), 1)

    def test_grids_are_not_assumed_monotonic(self):
        # Three temperature axes are declared descending; that is data, not an error.
        ascending = get_object(self.objects, "ldp_tco__iga_tco_ch_mt")
        grid = ascending.axis_labels("y")
        self.assertEqual(len(grid), 8)
        self.assertFalse(all(b >= a for a, b in zip(grid, grid[1:])))

    def test_text_label_axes_are_marked_and_left_unparsed(self):
        increments = get_object(self.objects, "DTC Table Increments")
        self.assertIn("x", increments.text_axes)
        self.assertNotIn("x", increments.labels)

    def test_only_two_distinct_stock_curves_exist(self):
        signatures = {}
        for path in sorted(FIRMWARE.glob("*.bin")):
            data = path.read_bytes()
            base = 0 if len(data) == 0x6000 else 0x14000
            values = read_engineering(data[base:base + 0x6000], self.maf)
            first = next(index for index, value in enumerate(values) if value > 0)
            signatures.setdefault((max(values), first), []).append(path.name)
        self.assertEqual(len(signatures), 2, signatures)
        self.assertEqual(sorted(signatures), [(518.0, 7), (725.09375, 10)])

    def test_stock_flow_at_the_documented_saturation_voltage(self):
        # 4.7 V falls between columns 238 (4.684 V) and 239 (4.704 V); the flow
        # below is the first column at or above 4.7 V.
        grid = self.maf.axis_labels("y")
        self.assertLess(grid[238], 4.7)
        self.assertGreater(grid[239], 4.7)
        expectations = {
            PARTIAL_B28: 622.46875,
            PARTIAL_B20: 449.078125,
        }
        for path, expected in expectations.items():
            values = read_engineering(path.read_bytes(), self.maf)
            self.assertAlmostEqual(values[239], expected, places=4, msg=path.name)
            # The curve is still climbing at 4.7 V: the sensor ceiling is the
            # sensor's own limit, not a plateau in the calibration.
            self.assertGreater(max(values), values[239])

    def test_column_of_a_voltage_is_where_the_flow_is_read(self):
        values = read_engineering(PARTIAL_B28.read_bytes(), self.maf)
        step = 5.018 / 255
        for voltage in (1.0, 2.5, 4.0):
            index = round(voltage / step)
            self.assertLessEqual(abs(self.maf.axis_labels("y")[index] - voltage), step)
            self.assertGreater(values[index], 0)


class PolicyTests(unittest.TestCase):
    def test_policy_loads_and_defaults_to_deny(self):
        policy = load_policy()
        self.assertIsNotNone(policy.entry("c_n_max"))
        self.assertIsNone(policy.entry("some_random_symbol"))

    def test_policy_limits_are_ordered(self):
        for symbol, entry in load_policy().entries.items():
            if entry.minimum is not None and entry.maximum is not None:
                self.assertLess(entry.minimum, entry.maximum, symbol)


class PlanTests(unittest.TestCase):
    def test_allowlisted_symbol_plans(self):
        _, _, plan, _ = build_plan(PARTIAL_B28, [{"symbol": "c_n_max", "value": 6700}])
        self.assertEqual(plan.status, "READY")
        self.assertEqual(len(plan.changes), 1)
        change = plan.changes[0]
        self.assertEqual(change.address, 0x1CA)
        self.assertEqual(change.byte_offsets, [0x1CA])
        self.assertEqual(change.old_raw, 203)

    def test_symbol_outside_allowlist_is_rejected(self):
        _, _, plan, _ = build_plan(
            PARTIAL_B28, [{"symbol": "ip_iga_max_knk__n__maf", "value": 1}]
        )
        self.assertEqual(plan.status, "REJECTED")
        self.assertIn("SYMBOL_NOT_ALLOWED",
                      {finding.code for finding in plan.findings})

    def test_unknown_symbol_is_rejected(self):
        _, _, plan, _ = build_plan(PARTIAL_B28, [{"symbol": "nope", "value": 1}])
        self.assertIn("SYMBOL_NOT_ALLOWED",
                      {finding.code for finding in plan.findings})

    def test_value_above_policy_maximum_is_rejected(self):
        _, _, plan, _ = build_plan(PARTIAL_B28, [{"symbol": "c_n_max", "value": 7500}])
        self.assertEqual(plan.status, "REJECTED")
        self.assertIn("POLICY_MAX_VIOLATION",
                      {finding.code for finding in plan.findings})

    def test_value_below_policy_minimum_is_rejected(self):
        _, _, plan, _ = build_plan(PARTIAL_B28, [{"symbol": "c_n_max", "value": 5000}])
        self.assertIn("POLICY_MIN_VIOLATION",
                      {finding.code for finding in plan.findings})

    def test_request_beyond_absolute_range_is_rejected(self):
        # Stock 6496 rpm, 32 rpm per count: 6899 rounds to 6912, over the 6900 max.
        _, _, plan, _ = build_plan(PARTIAL_B28, [{"symbol": "c_n_max", "value": 6899}])
        self.assertEqual(plan.status, "REJECTED")
        self.assertIn("POLICY_MAX_VIOLATION",
                      {finding.code for finding in plan.findings})

    def test_delta_rule_is_independent_of_the_absolute_limits(self):
        # The shipped policy is tighter on delta than on the absolute ceiling, so
        # exercise the delta rule with the ceiling lifted.
        policy = Policy(
            version=1,
            description="test",
            entries={
                "c_n_max": PolicyEntry("c_n_max", minimum=0, maximum=7100,
                                       max_delta_percent=6.0),
            },
        )
        _, _, plan, _ = build_plan(PARTIAL_B28, [{"symbol": "c_n_max", "value": 6900}],
                                   policy=policy)
        self.assertEqual(plan.status, "REJECTED")
        self.assertIn("DELTA_TOO_LARGE", {finding.code for finding in plan.findings})

    def test_real_policy_ceiling_is_the_delta_rule(self):
        # +6% of 6496 is 6885.8; the largest representable step below it is 6880.
        _, _, plan, _ = build_plan(PARTIAL_B28, [{"symbol": "c_n_max", "value": 6880}])
        self.assertEqual(plan.status, "READY")
        self.assertLess(plan.changes[0].delta_percent, 6.0)
        _, _, higher, _ = build_plan(PARTIAL_B28, [{"symbol": "c_n_max", "value": 6912}])
        self.assertEqual(higher.status, "REJECTED")

    def test_stock_zero_skips_the_relative_delta_rule(self):
        # Only c_n_max carries a delta rule today, so drive the zero-stock branch
        # with a policy that also constrains c_n_max_hys (stock 0 rpm on M52B20).
        policy = Policy(
            version=1,
            description="test",
            entries={
                "c_n_max_hys": PolicyEntry("c_n_max_hys", minimum=0, maximum=512,
                                           max_delta_percent=6.0),
            },
        )
        _, _, plan, _ = build_plan(PARTIAL_B20, [{"symbol": "c_n_max_hys", "value": 96}],
                                   policy=policy)
        self.assertEqual(plan.status, "READY")
        self.assertIn("DELTA_FROM_ZERO",
                      {finding.code for finding in plan.findings})
        self.assertIsNone(plan.changes[0].delta_percent)

    def test_coarse_quantization_is_rejected(self):
        # c_n_max_hys is 32 rpm per count; 100 rpm would land 4% away from the request.
        _, _, plan, _ = build_plan(PARTIAL_B28, [{"symbol": "c_n_max_hys", "value": 100}])
        self.assertEqual(plan.status, "REJECTED")
        self.assertIn("QUANTIZATION_TOO_COARSE",
                      {finding.code for finding in plan.findings})

    def test_quantization_error_is_relative_to_the_requested_value(self):
        # 100.4 km/h resolves to 100, i.e. 0.4% away, which the policy tolerates.
        _, _, plan, _ = build_plan(PARTIAL_B28, [{"symbol": "c_vs_max", "value": 100.4}])
        self.assertEqual(plan.status, "READY")
        self.assertEqual(plan.changes[0].new_raw, 100)
        self.assertAlmostEqual(plan.changes[0].quantization_error_percent, 0.3984, places=3)

    def test_representable_quantization_is_accepted(self):
        _, _, plan, _ = build_plan(PARTIAL_B28, [{"symbol": "c_vs_max", "value": 100}])
        self.assertEqual(plan.status, "READY")
        self.assertEqual(plan.changes[0].new_raw, 100)

    def test_index_out_of_range_is_rejected(self):
        _, _, plan, _ = build_plan(
            PARTIAL_B28, [{"symbol": MAF_TITLE, "index": 999, "value": 100}]
        )
        self.assertIn("INDEX_OUT_OF_RANGE",
                      {finding.code for finding in plan.findings})

    def test_constant_with_index_is_rejected(self):
        _, _, plan, _ = build_plan(
            PARTIAL_B28, [{"symbol": "c_n_max", "index": 3, "value": 6700}]
        )
        self.assertIn("REQUEST_INVALID", {finding.code for finding in plan.findings})

    def test_non_numeric_value_is_rejected(self):
        _, _, plan, _ = build_plan(PARTIAL_B28, [{"symbol": "c_n_max", "value": "high"}])
        self.assertIn("REQUEST_INVALID", {finding.code for finding in plan.findings})

    def test_duplicate_writes_overlap_and_are_rejected(self):
        _, _, plan, _ = build_plan(
            PARTIAL_B28,
            [{"symbol": "c_n_max", "value": 6700}, {"symbol": "c_n_max", "value": 6800}],
        )
        self.assertEqual(plan.status, "REJECTED")
        self.assertIn("OVERLAPPING_WRITES", {finding.code for finding in plan.findings})

    def test_write_outside_crc_coverage_is_rejected(self):
        # The block is 0x6000 long but the chain only protects the first 0x2D4A
        # bytes; a write past that would escape verification entirely.
        objects = load_objects()
        extended = dict(objects)
        shifted = dataclasses.replace(get_object(objects, "c_vs_max"), address=0x5000)
        extended["c_vs_max"] = [shifted]
        policy = Policy(
            version=1,
            description="test",
            entries={"c_vs_max": PolicyEntry("c_vs_max", minimum=0, maximum=255)},
        )
        _, _, plan, _ = build_plan(PARTIAL_B28, [{"symbol": "c_vs_max", "value": 200}],
                                   policy=policy, objects=extended)
        self.assertEqual(plan.status, "REJECTED")
        self.assertIn("OUTSIDE_CRC_COVERAGE",
                      {finding.code for finding in plan.findings})

    def test_empty_request_list_is_empty_not_ready(self):
        _, _, plan, _ = build_plan(PARTIAL_B28, [])
        self.assertEqual(plan.status, "EMPTY")
        self.assertEqual(plan.changes, [])


class TablePlanTests(unittest.TestCase):
    def maf_neighbours(self, path, index):
        objects = load_objects()
        obj = get_object(objects, MAF_TITLE)
        data = path.read_bytes()
        base = 0 if len(data) == 0x6000 else 0x14000
        step = obj.element_size_bytes
        read = lambda i: int.from_bytes(  # noqa: E731
            data[base + obj.address + i * step:base + obj.address + (i + 1) * step],
            "little",
        )
        return obj, read(index - 1), read(index), read(index + 1)

    def test_monotonic_preserving_table_edit_is_accepted(self):
        obj, before, current, after = self.maf_neighbours(PARTIAL_B28, 30)
        target = obj.to_engineering((before + current) // 2) if before < current else None
        if target is None:
            self.skipTest("neighbour layout does not allow a midpoint")
        _, _, plan, _ = build_plan(
            PARTIAL_B28, [{"symbol": MAF_TITLE, "index": 30, "value": target}]
        )
        self.assertEqual(plan.status, "READY")

    def test_table_edit_that_breaks_monotonicity_is_rejected(self):
        obj, before, current, after = self.maf_neighbours(PARTIAL_B28, 30)
        below = obj.to_engineering(max(before - 10, 0))
        _, _, plan, _ = build_plan(
            PARTIAL_B28, [{"symbol": MAF_TITLE, "index": 30, "value": below}]
        )
        self.assertEqual(plan.status, "REJECTED")
        self.assertIn("MONOTONICITY_BROKEN",
                      {finding.code for finding in plan.findings})

    def test_too_many_cells_in_one_patch_is_rejected(self):
        # The shipped policy lets the MAF view take its whole 256-column curve, so
        # the cell limit is exercised with a tighter policy.
        policy = Policy(
            version=1,
            description="test",
            entries={
                MAF_TITLE: PolicyEntry(MAF_TITLE, minimum=0, maximum=800,
                                       max_cells_per_patch=4),
            },
        )
        requests = [
            {"symbol": MAF_TITLE, "index": index, "value": 100.0}
            for index in range(20)
        ]
        _, _, plan, _ = build_plan(PARTIAL_B28, requests, policy=policy)
        self.assertIn("TOO_MANY_CELLS", {finding.code for finding in plan.findings})

    def test_full_maf_curve_is_allowed_by_the_shipped_policy(self):
        entry = load_policy().entry(MAF_TITLE)
        self.assertEqual(entry.max_cells_per_patch, 256)


class FixtureProvenanceTests(unittest.TestCase):
    def test_same_variant_pair_differs_only_outside_crc_coverage(self):
        # Two files carry variant 23414104 yet are not byte-identical: the only
        # differences sit at 0x5DFE..0x5E21, i.e. past the end of the CRC chain,
        # so neither file is damaged and both chains stay valid.
        full = (FIRMWARE / "MS41_1429861_E36_320i_M52B20_0060041010000_full_A.bin"
                ).read_bytes()[0x14000:0x1A000]
        partial = (FIRMWARE / "MS41_1429861_E36_320i_M52B20_1996_4101_partial.bin"
                   ).read_bytes()
        changed = [i for i, (a, b) in enumerate(zip(full, partial)) if a != b]
        self.assertTrue(changed)
        self.assertGreater(min(changed), crc_covered_end(partial))
        self.assertEqual(changed, list(range(0x5DFE, 0x5E22)))
        self.assertEqual(full[0x5DFE:0x5E22], b"\xff" * 36)
        self.assertTrue(verify_crc_chain(full)[0])
        self.assertTrue(verify_crc_chain(partial)[0])


class ApplyTests(unittest.TestCase):
    def test_change_bytes_crc_and_verify(self):
        image, offset, plan, objects = build_plan(
            PARTIAL_B28, [{"symbol": "c_n_max", "value": 6700},
                          {"symbol": "c_n_max_hys", "value": 96}]
        )
        self.assertEqual(plan.status, "READY")
        output, blocks = apply_plan(image, offset, plan, objects)
        self.assertEqual(blocks, 15)
        unplanned, findings = verify_application(image, output, offset, plan, objects)
        self.assertEqual(unplanned, [])
        self.assertFalse([f for f in findings if f.severity == "reject"])

        calibration = output[offset:offset + 0x6000]
        ok, blocks, errors = verify_crc_chain(calibration)
        self.assertTrue(ok, errors)
        self.assertEqual(blocks, 15)

        obj = get_object(objects, "c_n_max")
        self.assertAlmostEqual(read_engineering(bytes(calibration), obj)[0], 6688.0)
        self.assertNotEqual(calibration[0x1CA], image[offset + 0x1CA])

    def test_only_planned_and_crc_bytes_change(self):
        image, offset, plan, objects = build_plan(
            PARTIAL_B28, [{"symbol": "c_n_max", "value": 6700}]
        )
        output, _ = apply_plan(image, offset, plan, objects)
        changed = {i for i, (a, b) in enumerate(zip(image, output)) if a != b}
        allowed = {offset + 0x1CA} | {offset + o for o in crc_storage_offsets(
            bytes(image[offset:offset + 0x6000]))}
        self.assertTrue(changed <= allowed, [hex(i) for i in changed - allowed])
        self.assertEqual(len(changed), 3)  # one value byte, two CRC bytes

    def test_256k_full_image_keeps_program_untouched(self):
        image, offset, plan, objects = build_plan(
            FULL_B28, [{"symbol": "c_n_max", "value": 6700}]
        )
        output, _ = apply_plan(image, offset, plan, objects)
        changed = [i for i, (a, b) in enumerate(zip(image, output)) if a != b]
        self.assertTrue(all(0x14000 <= i < 0x14000 + 0x6000 for i in changed),
                        [hex(i) for i in changed])
        self.assertEqual(bytes(image[:0x14000]), bytes(output[:0x14000]))
        self.assertEqual(bytes(image[0x1A000:]), bytes(output[0x1A000:]))

    def test_round_trip_value_matches_request(self):
        for symbol, value in (("c_n_max", 6720), ("c_vs_max", 180), ("c_tco_min_pu", 60.75)):
            image, offset, plan, objects = build_plan(
                PARTIAL_B28, [{"symbol": symbol, "value": value}]
            )
            self.assertEqual(plan.status, "READY", symbol)
            output, _ = apply_plan(image, offset, plan, objects)
            obj = get_object(objects, symbol)
            read = read_engineering(bytes(output[offset:offset + 0x6000]), obj)[0]
            self.assertAlmostEqual(read, plan.changes[0].new_engineering, places=6)
            self.assertLess(plan.changes[0].quantization_error_percent, 1.0)

    def test_tampered_output_is_detected(self):
        image, offset, plan, objects = build_plan(
            PARTIAL_B28, [{"symbol": "c_n_max", "value": 6700}]
        )
        output, _ = apply_plan(image, offset, plan, objects)
        output[offset + 0x900] ^= 0xFF
        unplanned, findings = verify_application(image, output, offset, plan, objects)
        self.assertTrue(unplanned)
        codes = {finding.code for finding in findings}
        self.assertIn("UNPLANNED_BYTE_CHANGES", codes)
        self.assertIn("CRC_INVALID_AFTER_PATCH", codes)

    def test_rejected_plan_cannot_be_applied(self):
        image, offset, plan, objects = build_plan(
            PARTIAL_B28, [{"symbol": "c_n_max", "value": 9999}]
        )
        self.assertEqual(plan.status, "REJECTED")
        with self.assertRaises(PatchError):
            apply_plan(image, offset, plan, objects)

    def test_invalid_source_crc_is_rejected(self):
        image, offset, plan, objects = build_plan(
            PARTIAL_B28, [{"symbol": "c_n_max", "value": 6700}]
        )
        broken = bytearray(image)
        broken[offset + 0x1000] ^= 0x5A
        _, broken_offset, broken_type = load_calibration(PARTIAL_B28)
        _, problems = load_definition()
        broken_plan = plan_patches(
            broken, broken_offset, broken_type, objects, problems,
            [{"symbol": "c_n_max", "value": 6700}], load_policy(),
        )
        self.assertEqual(broken_plan.status, "REJECTED")
        self.assertIn("SOURCE_CRC_INVALID",
                      {finding.code for finding in broken_plan.findings})

    def test_unsupported_binary_is_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "junk.bin"
            path.write_bytes(b"\x00" * 0x6000)
            with self.assertRaises(PatchError):
                load_calibration(path)

    def test_every_fixture_can_be_patched_and_stays_valid(self):
        policy = load_policy()
        objects = load_objects()
        _, problems = load_definition()
        for path in sorted(FIRMWARE.glob("*.bin")):
            image, offset, image_type = load_calibration(path)
            plan = plan_patches(
                image, offset, image_type, objects, problems,
                [{"symbol": "c_vs_max", "value": 200}], policy,
            )
            self.assertEqual(plan.status, "READY", path.name)
            output, _ = apply_plan(image, offset, plan, objects)
            unplanned, findings = verify_application(image, output, offset, plan, objects)
            self.assertEqual(unplanned, [], path.name)
            self.assertFalse([f for f in findings if f.severity == "reject"], path.name)
            obj = get_object(objects, "c_vs_max")
            self.assertAlmostEqual(
                read_engineering(bytes(output[offset:offset + 0x6000]), obj)[0], 200.0
            )


class CrcTests(unittest.TestCase):
    def test_recompute_is_idempotent(self):
        data = bytearray(PARTIAL_B28.read_bytes())
        self.assertEqual(recompute_crc(data), 15)
        again = bytearray(data)
        self.assertEqual(recompute_crc(again), 15)
        self.assertEqual(bytes(data), bytes(again))

    def test_recompute_repairs_a_broken_value_byte(self):
        data = bytearray(PARTIAL_B28.read_bytes())
        data[0x1CA] ^= 0x01
        self.assertFalse(verify_crc_chain(data)[0])
        recompute_crc(data)
        self.assertTrue(verify_crc_chain(data)[0])

    def test_crc_storage_offsets_match_the_chain(self):
        data = PARTIAL_B28.read_bytes()
        offsets = crc_storage_offsets(data)
        self.assertEqual(len(offsets), 15 * 2)
        for offset in offsets[::2]:
            recorded = int.from_bytes(data[offset:offset + 2], "little")
            self.assertNotEqual(recorded, 0xFFFF)

    def test_coverage_ends_at_the_last_checksum_address_in_every_fixture(self):
        for path in sorted(FIRMWARE.glob("*.bin")):
            data = path.read_bytes()
            base = 0 if len(data) == 0x6000 else 0x14000
            self.assertEqual(crc_covered_end(data[base:base + 0x6000]), 0x2D4A, path.name)

    def test_full_dump_chain_sits_at_the_calibration_offset(self):
        data = FULL_B28.read_bytes()
        self.assertEqual(len(data), 0x40000)
        ok, blocks, errors = verify_crc_chain(data[0x14000:0x1A000])
        self.assertTrue(ok, errors)
        self.assertEqual(blocks, 15)
        # Offset 0 holds program code, so it cannot carry a valid chain.
        self.assertFalse(verify_crc_chain(data[0:0x6000])[0])
        _, offset, image_type = load_calibration(FULL_B28)
        self.assertEqual(offset, 0x14000)
        self.assertEqual(image_type, "full-flash-256K")

    def test_chain_covers_every_byte_before_the_terminator(self):
        data = PARTIAL_B28.read_bytes()
        initial = int.from_bytes(data[0x0E:0x10], "big")
        cursor = 0
        covered = []
        while True:
            checksum_address = int.from_bytes(data[cursor:cursor + 2], "little")
            if checksum_address == 0xFFFF:
                break
            covered.extend(range(cursor, checksum_address))
            cursor = checksum_address + 2
        self.assertEqual(cursor, 0x2D4C)
        gaps = [i for i in range(0, 0x2D4C) if i not in set(covered)]
        # Every byte of the calibration region is covered except the 15 stored
        # checksum words themselves; all 15 chain pointers fall inside a run.
        self.assertEqual(len(gaps), 15 * 2)
        crc16 = _crc16(data[0:78], initial)
        self.assertEqual(crc16, int.from_bytes(data[78:80], "little"))


if __name__ == "__main__":
    unittest.main()
