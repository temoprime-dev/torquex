import json
import subprocess
import sys
import unittest
from pathlib import Path

from MS41.tools.calibration import crc_covered_end
from MS41.tools.table_layout import (
    DECLARED_TRENDS,
    STRICT_TRENDS,
    describe,
    load_axis_providers,
)
from MS41.tools.xdf import XdfError, get_object, load_definition

ROOT = Path(__file__).parents[2]
FIRMWARE = ROOT / "MS41" / "firmware"
PARTIAL_B28 = FIRMWARE / "MS41_1429861_E36_328i_M52B28_1997_4101_partial.bin"
FULL_B28 = FIRMWARE / "MS41_1429861_E39_528i_M52B28_1995_0060041010000_full.bin"
XDF = ROOT / "MS41" / "definitions" / "Siemens_MS41_1429861_ENG_24K_v1.0.xdf"
TOOL = ROOT / "MS41" / "tools" / "table_layout.py"

INJECTION_MAPS = ("ip_tib__n__maf", "ip_ti_ivvt__n__maf")
IGNITION_MAPS = ("ip_igab__n__maf", "ip_iga_ivvt__n__maf", "ip_igab_cat_var__n__maf")


class ProviderTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.providers = load_axis_providers(XDF)
        cls.objects = load_definition(XDF)[0]

    def test_companions_are_found_for_the_16x12_family(self):
        for title in INJECTION_MAPS + IGNITION_MAPS:
            candidates = {candidate.title for candidate in self.providers[title]}
            self.assertIn("ldpm_maf_1_3", candidates, title)
            self.assertIn("ldpm_n_2_3", candidates, title)

    def test_candidates_carry_points_and_units(self):
        by_title = {candidate.title: candidate
                    for candidate in self.providers["ip_tib__n__maf"]}
        load_axis = by_title["ldpm_maf_1_3"]
        speed_axis = by_title["ldpm_n_2_3"]
        self.assertEqual(load_axis.points, 12)
        self.assertEqual(load_axis.units, "mg/stroke")
        self.assertEqual(speed_axis.points, 16)
        self.assertEqual(speed_axis.units, "rpm")

    def test_the_companions_are_real_objects_with_addresses(self):
        load_axis = get_object(self.objects, "ldpm_maf_1_3")
        speed_axis = get_object(self.objects, "ldpm_n_2_3")
        self.assertEqual(load_axis.address, 0x77C)
        self.assertEqual(speed_axis.address, 0x796)


class InjectionMapLayoutTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = describe("ip_tib__n__maf", PARTIAL_B28)

    def test_shape_and_span(self):
        report = self.report
        self.assertEqual((report.rows, report.columns), (16, 12))
        self.assertEqual(report.element_size_bits, 16)
        self.assertEqual(report.data_address, 0x9F8)
        self.assertEqual(report.data_end, 0xB78)
        self.assertLess(report.data_end, 0x2D4A)
        self.assertEqual(report.units, "ms")

    def test_data_stays_inside_the_crc_protected_region(self):
        covered = crc_covered_end(PARTIAL_B28.read_bytes())
        self.assertLessEqual(self.report.data_end, covered)

    def test_axes_are_the_companion_arrays(self):
        axes = {axis.role: axis for axis in self.report.axes}
        self.assertEqual(axes["load"].provider, "ldpm_maf_1_3")
        self.assertEqual(axes["load"].address, 0x77C)
        self.assertEqual(len(axes["load"].grid), 12)
        self.assertAlmostEqual(axes["load"].grid[0], 50.0108, places=3)
        self.assertAlmostEqual(axes["load"].grid[-1], 700.172, places=3)
        self.assertEqual(axes["rpm"].provider, "ldpm_n_2_3")
        self.assertEqual(axes["rpm"].address, 0x796)
        self.assertEqual(axes["rpm"].grid[0], 640.0)
        self.assertEqual(axes["rpm"].grid[-1], 6500.0)

    def test_both_axes_are_strictly_increasing(self):
        for axis in self.report.axes:
            self.assertTrue(all(b > a for a, b in zip(axis.grid, axis.grid[1:])),
                            axis.provider)

    def test_orientation_is_decided_by_the_data(self):
        self.assertEqual(self.report.orientation, "rpm_major")
        self.assertLess(self.report.orientation_violations["rpm_major"],
                        self.report.orientation_violations["transposed"])

    def test_a_proven_layout_has_no_problems(self):
        self.assertEqual(self.report.problems, [])
        self.assertTrue(self.report.ok)

    def test_injector_time_rises_with_load(self):
        values = [1.26, 1.80, 3.05, 4.31, 6.15, 7.98, 9.55, 10.81, 11.41, 12.63,
                  14.71, 16.28]
        self.assertTrue(all(b >= a for a, b in zip(values, values[1:])))
        self.assertAlmostEqual(self.report.value_min, 0.875, places=3)
        self.assertAlmostEqual(self.report.value_max, 17.072, places=3)

    def test_ignition_maps_share_the_same_grid(self):
        for title in IGNITION_MAPS:
            report = describe(title, PARTIAL_B28)
            roles = {axis.role: axis for axis in report.axes}
            self.assertEqual(roles["load"].provider, "ldpm_maf_1_3", title)
            self.assertEqual(roles["rpm"].provider, "ldpm_n_2_3", title)
            self.assertTrue(report.ok, f"{title}: {report.problems}")


class OrientationAndTrendTests(unittest.TestCase):
    def test_declared_trends_only_cover_known_families(self):
        for title, trend in DECLARED_TRENDS.items():
            self.assertTrue(trend, title)
            self.assertTrue(set(trend) <= {"load", "rpm"}, title)
        self.assertEqual(set(STRICT_TRENDS), {"ip_tib__n__maf"})

    def test_maps_without_a_declared_trend_are_left_unchecked(self):
        # The VANOS injection map is a correction that oscillates around zero, so
        # its orientation is not claimed either way.
        report = describe("ip_ti_ivvt__n__maf", PARTIAL_B28)
        self.assertEqual(report.orientation, "not checked")
        self.assertEqual(report.orientation_violations, {})

    def test_axis_role_follows_the_point_count_not_the_order(self):
        # 9 rows x 8 columns: the 8-point companion is the load axis even though it
        # is declared second.
        report = describe("ip_tqr_max__n__maf", PARTIAL_B28)
        roles = {axis.role: axis for axis in report.axes}
        self.assertEqual((report.rows, report.columns), (9, 8))
        self.assertEqual(roles["load"].provider, "ldpm_maf_2_3")
        self.assertEqual(len(roles["load"].grid), 8)
        self.assertEqual(roles["rpm"].provider, "ldpm_n_5_3")
        self.assertEqual(len(roles["rpm"].grid), 9)

    def test_smaller_map_uses_its_own_companions(self):
        report = describe("ip_igab_is__n__maf", PARTIAL_B28)
        roles = {axis.role: axis for axis in report.axes}
        self.assertEqual(roles["load"].provider, "ldp_maf__igab_is")
        self.assertEqual(roles["rpm"].provider, "ldp_n__igab_is")
        self.assertEqual(roles["rpm"].grid[0], 480.0)

    def test_a_map_without_companions_reports_the_gap(self):
        report = describe("ip_isapwm_accin__tq_accin_can__tia", PARTIAL_B28)
        self.assertFalse(report.ok)
        self.assertTrue(any("axis" in problem for problem in report.problems))

    def test_unknown_symbol_raises(self):
        with self.assertRaises(XdfError):
            describe("no_such_map", PARTIAL_B28)


class FixtureSweepTests(unittest.TestCase):
    def test_every_fixture_has_the_same_injection_layout(self):
        for path in sorted(FIRMWARE.glob("*.bin")):
            report = describe("ip_tib__n__maf", path)
            self.assertTrue(report.ok, f"{path.name}: {report.problems}")
            self.assertEqual((report.rows, report.columns), (16, 12), path.name)
            self.assertEqual(report.orientation, "rpm_major", path.name)

    def test_addresses_are_calibration_relative_for_both_image_types(self):
        # The XDF addresses are relative to the calibration block; a full image
        # puts that block at 0x14000, which load_calibration already accounts for.
        for path in (PARTIAL_B28, FULL_B28):
            report = describe("ip_tib__n__maf", path)
            self.assertEqual(report.data_address, 0x9F8, path.name)
            self.assertEqual(report.data_end, 0xB78, path.name)

    def test_both_bodies_share_one_grid_in_every_fixture(self):
        for path in sorted(FIRMWARE.glob("*.bin")):
            b20 = describe("ip_tib__n__maf", path)
            grids = [tuple(axis.grid) for axis in b20.axes]
            self.assertEqual(len(grids), 2, path.name)
            for other in IGNITION_MAPS + ("ip_ti_ivvt__n__maf",):
                other_report = describe(other, path)
                other_grids = [tuple(axis.grid) for axis in other_report.axes]
                self.assertEqual(grids, other_grids,
                                 f"{path.name}: {other} uses a different grid")


class CliTests(unittest.TestCase):
    def test_cli_reports_success_for_the_declared_families(self):
        completed = subprocess.run(
            [sys.executable, str(TOOL), "--bin", str(PARTIAL_B28)],
            capture_output=True, text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("ip_tib__n__maf", completed.stdout)
        self.assertIn("3/3 maps described without problems", completed.stdout)

    def test_cli_json_output_is_machine_readable(self):
        completed = subprocess.run(
            [sys.executable, str(TOOL), "--bin", str(PARTIAL_B28),
             "--symbol", "ip_tib__n__maf", "--json"],
            capture_output=True, text=True,
        )
        self.assertEqual(completed.returncode, 0, completed.stderr)
        payload = json.loads(completed.stdout)[0]
        self.assertEqual(payload["symbol"], "ip_tib__n__maf")
        self.assertEqual(payload["data_address"], "0x09F8")
        self.assertEqual(payload["orientation"], "rpm_major")
        self.assertEqual([axis["provider"] for axis in payload["axes"]],
                         ["ldpm_maf_1_3", "ldpm_n_2_3"])


if __name__ == "__main__":
    unittest.main()
