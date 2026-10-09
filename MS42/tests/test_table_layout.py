import json
import unittest
from pathlib import Path

from MS42.tools.table_layout import (
    CRC_COVERED_END_RELATIVE,
    CRC_COVERED_LAST,
    LOAD_TRENDS,
    MAX_RPM,
    describe,
    load_axis_providers,
)
from MS42.tools.xdf import XdfError, load_definition

ROOT = Path(__file__).parents[2]
FIRMWARE = ROOT / "MS42" / "firmware"
FILES = sorted(FIRMWARE.glob("*.bin"))
B28_LHD = FIRMWARE / "Siemens_MS42_0110C6_E46_M52TUB28_EU3_LHD.bin"

OBJECTS, PROBLEMS = load_definition()
PROVIDERS = load_axis_providers()


class ProviderTests(unittest.TestCase):
    def test_definition_parses_without_problems(self):
        self.assertEqual([], PROBLEMS)

    def test_ip_tib_has_both_axis_providers(self):
        candidates = {candidate.title: candidate for candidate in PROVIDERS["ip_tib__n__maf"]}
        self.assertEqual({"ldpm_n_4_7", "ldpm_maf_4_7"}, set(candidates))
        self.assertTrue(candidates["ldpm_n_4_7"].is_rpm)
        self.assertFalse(candidates["ldpm_maf_4_7"].is_rpm)
        self.assertEqual(16, candidates["ldpm_n_4_7"].points)
        self.assertEqual(12, candidates["ldpm_maf_4_7"].points)

    def test_rpm_providers_are_recognised_by_name_and_by_units(self):
        by_name = {c.title: c for c in PROVIDERS["ip_ti_pvs__pvs__n"]}
        self.assertTrue(by_name["ldpm_n_21_7"].is_rpm)          # name convention
        self.assertFalse(by_name["ldpm_pvs_21_7"].is_rpm)
        self.assertTrue(by_name["ldpm_n_21_7"].points == 8)

    def test_every_declared_trend_symbol_exists_with_two_axes(self):
        for symbol, trend in LOAD_TRENDS.items():
            with self.subTest(symbol=symbol):
                self.assertIn(symbol, OBJECTS)
                self.assertIn(symbol, PROVIDERS)
                self.assertIn(trend["axis"], ("rows", "columns"))
                self.assertIn(trend["direction"], ("up", "down"))
                self.assertIn(trend["rpm"], ("rows", "columns"))

    def test_all_xdf_objects_lie_inside_the_crc_protected_block(self):
        """A map past 0x4F843 could be edited without the checksum noticing."""
        worst = max(objects[0].address + objects[0].byte_length
                    for objects in OBJECTS.values())
        self.assertLessEqual(worst, CRC_COVERED_END_RELATIVE)
        self.assertEqual(0x4F843, CRC_COVERED_LAST)


class DeclaredMapTests(unittest.TestCase):
    """Every declared tendency must hold on every stock image."""

    def test_declared_maps_describe_without_problems(self):
        for path in FILES:
            for symbol in sorted(LOAD_TRENDS):
                with self.subTest(path=path.name, symbol=symbol):
                    report = describe(symbol, path, objects=OBJECTS, providers=PROVIDERS)
                    self.assertTrue(report.ok, report.problems)
                    self.assertEqual("row_major", report.orientation)
                    self.assertEqual(
                        report.orientation_violations["row_major"], 0, report.to_json())
                    self.assertLess(
                        report.orientation_violations["row_major"],
                        report.orientation_violations["transposed"],
                    )

    def test_declared_maps_have_two_axes_in_plausible_ranges(self):
        report = describe("ip_tib__n__maf", B28_LHD, objects=OBJECTS, providers=PROVIDERS)
        roles = {axis.axis: axis.role for axis in report.axes}
        self.assertEqual({"rows": "rpm", "columns": "load"}, roles)
        rpm = next(axis for axis in report.axes if axis.role == "rpm")
        self.assertLessEqual(rpm.grid[-1], MAX_RPM)
        self.assertTrue(any(marker in rpm.units for marker in ("1/min", "min-1", "U/min")))

    def test_a_map_with_rpm_on_the_columns_is_read_that_way(self):
        report = describe("ip_ti_pvs__pvs__n", B28_LHD, objects=OBJECTS, providers=PROVIDERS)
        self.assertTrue(report.ok, report.problems)
        roles = {axis.axis: axis.role for axis in report.axes}
        self.assertEqual({"columns": "rpm", "rows": "load"}, roles)

    def test_a_column_rpm_map_still_resolves_the_storage_order(self):
        report = describe("ip_fac_isapwm_cor__pvs_cor__n", B28_LHD,
                          objects=OBJECTS, providers=PROVIDERS)
        self.assertTrue(report.ok, report.problems)
        self.assertEqual("row_major", report.orientation)
        self.assertEqual(0, report.orientation_violations["row_major"])

    def test_maps_without_a_declared_trend_say_so(self):
        report = describe("id_knkwb_0__n__maf", B28_LHD, objects=OBJECTS, providers=PROVIDERS)
        self.assertEqual("not checked", report.orientation)
        self.assertEqual({}, report.orientation_violations)

    def test_a_map_without_axis_providers_reports_a_problem(self):
        report = describe("id_maf_tab", B28_LHD, objects=OBJECTS, providers=PROVIDERS)
        self.assertFalse(report.ok)
        self.assertTrue(any("no axis provider" in problem for problem in report.problems))

    def test_unknown_symbol_is_an_error(self):
        with self.assertRaises(XdfError):
            describe("kf_not_a_map", B28_LHD, objects=OBJECTS, providers=PROVIDERS)


class ReportShapeTests(unittest.TestCase):
    def test_json_is_serialisable_and_complete(self):
        report = describe("ip_tib__n__maf", B28_LHD, objects=OBJECTS, providers=PROVIDERS)
        payload = json.loads(json.dumps(report.to_json()))
        self.assertEqual("0x482E", payload["data_address"])
        self.assertEqual("0x49AE", payload["data_end"])
        self.assertTrue(payload["ok"])
        self.assertEqual(2, len(payload["axes"]))
        self.assertEqual(0, payload["orientation_violations"]["row_major"])

    def test_data_end_is_exclusive(self):
        report = describe("ip_tib__n__maf", B28_LHD, objects=OBJECTS, providers=PROVIDERS)
        self.assertEqual(report.data_address + report.rows * report.columns
                         * (report.element_size_bits // 8), report.data_end)


if __name__ == "__main__":
    unittest.main()
